# Copyright 2015 rpaas authors. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

import copy
import datetime
import logging
import os
import sys

from celery import Celery, Task
import redis
import hm.managers.cloudstack  # NOQA
import hm.lb_managers.cloudstack  # NOQA
import hm.lb_managers.networkapi_cloudstack  # NOQA

from hm import config
from hm.model.host import Host
from hm.model.load_balancer import LoadBalancer

from rpaas import consul_manager, hc, nginx, ssl, ssl_plugins, storage


redis_host = os.environ.get('REDIS_HOST', 'localhost')
redis_port = os.environ.get('REDIS_PORT', '6379')
redis_password = os.environ.get('REDIS_PASSWORD', '')
auth_prefix = ''
if redis_password:
    auth_prefix = ':{}@'.format(redis_password)
redis_broker = "redis://{}{}:{}/0".format(auth_prefix, redis_host, redis_port)
app = Celery('tasks', broker=redis_broker, backend=redis_broker)
app.conf.update(
    CELERY_TASK_SERIALIZER='json',
    CELERY_RESULT_SERIALIZER='json',
    CELERY_ACCEPT_CONTENT=['json'],
)

ssl_plugins.register_plugins()


class NotReadyError(Exception):
    pass


class TaskNotFoundError(Exception):
    pass


class TaskManager(object):

    def __init__(self, config=None):
        self.storage = storage.MongoDBStorage(config)

    def ensure_ready(self, name):
        task = self.storage.find_task(name)
        if task.count() >= 1:
            raise NotReadyError("Async task still running")

    def remove(self, name):
        try:
            self.ensure_ready(name)
        except NotReadyError:
            self.storage.remove_task(name)
        else:
            raise TaskNotFoundError("Task {} not found for removal".format(name))

    def create(self, name):
        self.storage.store_task(name)

    def update(self, name, task_id):
        self.storage.update_task(name, task_id)


class BaseManagerTask(Task):
    ignore_result = True
    store_errors_even_if_ignored = True

    def init_config(self, config=None):
        self.config = config
        self.nginx_manager = nginx.Nginx(config)
        self.consul_manager = consul_manager.ConsulManager(config)
        self.host_manager_name = self._get_conf("HOST_MANAGER", "cloudstack")
        self.lb_manager_name = self._get_conf("LB_MANAGER", "networkapi_cloudstack")
        self.task_manager = TaskManager(config)
        self.redis_client = redis.StrictRedis(host=redis_host, port=redis_port, password=redis_password,
                                              socket_timeout=3)
        self.hc = hc.Dumb()
        self.storage = storage.MongoDBStorage(config)
        hc_url = self._get_conf("HCAPI_URL", None)
        if hc_url:
            self.hc = hc.HCAPI(self.storage,
                               url=hc_url,
                               user=self._get_conf("HCAPI_USER"),
                               password=self._get_conf("HCAPI_PASSWORD"),
                               hc_format=self._get_conf("HCAPI_FORMAT", "http://{}:8080/"))

    def _get_conf(self, key, default=config.undefined):
        return config.get_config(key, default, self.config)

    def _add_host(self, name, lb=None):
        healthcheck_timeout = int(self._get_conf("RPAAS_HEALTHCHECK_TIMEOUT", 600))
        host = Host.create(self.host_manager_name, name, self.config)
        created_lb = None
        try:
            if not lb:
                lb = created_lb = LoadBalancer.create(self.lb_manager_name, name, self.config)
            lb.add_host(host)
            self.nginx_manager.wait_healthcheck(host.dns_name, timeout=healthcheck_timeout)
            self.hc.create(name)
            self.hc.add_url(name, host.dns_name)
            self.storage.remove_task(name)
        except:
            exc_info = sys.exc_info()
            rollback = self._get_conf("RPAAS_ROLLBACK_ON_ERROR", "0") in ("True", "true", "1")
            if not rollback:
                raise
            try:
                if created_lb is not None:
                    created_lb.destroy()
            except Exception as e:
                logging.error("Error in rollback trying to destroy load balancer: {}".format(e))
            try:
                self._delete_host(host)
            except Exception as e:
                logging.error("Error in rollback trying to destroy host: {}".format(e))
            try:
                self.hc.destroy(name)
            except Exception as e:
                logging.error("Error in rollback trying to remove healthcheck: {}".format(e))
            raise exc_info[0], exc_info[1], exc_info[2]

    def _delete_host(self, host, lb=None):
        host.destroy()
        if lb is not None:
            lb.remove_host(host)
        node_name = None
        for node in self.consul_manager.list_node():
            if node['Address'] == host.dns_name:
                node_name = node['Node']
        if node_name is not None:
            self.consul_manager.remove_node(node_name)
        if lb is not None:
            self.hc.remove_url(lb.name, host.dns_name)


class NewInstanceTask(BaseManagerTask):

    def run(self, config, name):
        self.init_config(config)
        self._add_host(name)


class RemoveInstanceTask(BaseManagerTask):

    def run(self, config, name):
        self.init_config(config)
        lb = LoadBalancer.find(name, self.config)
        if lb is None:
            raise storage.InstanceNotFoundError()
        for host in lb.hosts:
            self._delete_host(host, lb)
        lb.destroy()
        self.hc.destroy(name)


class ScaleInstanceTask(BaseManagerTask):

    def run(self, config, name, quantity):
        try:
            self.init_config(config)
            lb = LoadBalancer.find(name, self.config)
            if lb is None:
                raise storage.InstanceNotFoundError()
            diff = quantity - len(lb.hosts)
            if diff == 0:
                return
            for i in xrange(abs(diff)):
                if diff > 0:
                    self._add_host(name, lb=lb)
                else:
                    self._delete_host(lb.hosts[i], lb)
        finally:
            self.storage.remove_task(name)


class RestoreMachineTask(BaseManagerTask):

    def run(self, config):
        self.init_config(config)
        lock_name = self.config.get("RESTORE_LOCK_NAME", "restore_lock")
        healthcheck_timeout = int(self._get_conf("RPAAS_HEALTHCHECK_TIMEOUT", 600))
        restore_delay = int(self.config.get("RESTORE_MACHINE_DELAY", 5))
        created_in = datetime.datetime.utcnow() - datetime.timedelta(minutes=restore_delay)
        restore_query = {"_id": {"$regex": "restore_.+"}, "created": {"$lte": created_in}}
        if self._redis_lock(lock_name, timeout=(healthcheck_timeout + 60)):
            for task in self.storage.find_task(restore_query):
                try:
                    start_time = datetime.datetime.utcnow()
                    self._restore_machine(task, config, healthcheck_timeout)
                    elapsed_time = datetime.datetime.utcnow() - start_time
                    self._redis_extend_lock(extra_time=elapsed_time.seconds)
                except Exception as e:
                    self.storage.update_task(task['_id'], {"last_attempt": datetime.datetime.utcnow()})
                    self._redis_unlock()
                    raise e
            self._redis_unlock()

    def _restore_machine(self, task, config, healthcheck_timeout):
        retry_failure_delay = int(self.config.get("RESTORE_MACHINE_FAILURE_DELAY", 5))
        restore_dry_mode = self.config.get("RESTORE_MACHINE_DRY_MODE", False)
        retry_failure_query = {"_id": {"$regex": "restore_.+"}, "last_attempt": {"$ne": None}}
        if task['instance'] not in self._failure_instances(retry_failure_query, retry_failure_delay):
            host = self.storage.find_host_id(task['host'])
            if not restore_dry_mode:
                Host.from_dict({"_id": host['_id'], "dns_name": task['host'],
                                "manager": host['manager']}, conf=config).restore()
                self.nginx_manager.wait_healthcheck(task['host'], timeout=healthcheck_timeout)
            self.storage.remove_task({"_id": task['_id']})

    def _failure_instances(self, retry_failure_query, retry_failure_delay):
        failure_instances = set()
        for task in self.storage.find_task(retry_failure_query):
            retry_failure = task['last_attempt'] + datetime.timedelta(minutes=retry_failure_delay)
            if (retry_failure >= datetime.datetime.utcnow()):
                failure_instances.add(task['instance'])
        return failure_instances

    def _redis_lock(self, lock_name, timeout):
        self.redis_lock = self.redis_client.lock(name=lock_name, timeout=timeout,
                                                 blocking_timeout=1)
        return self.redis_lock.acquire(blocking=False)

    def _redis_unlock(self):
        self.redis_lock.release()

    def _redis_extend_lock(self, extra_time):
        self.redis_lock.extend(extra_time)


class CheckMachineTask(BaseManagerTask):

    def run(self, config):
        self.init_config(config)
        for node in self.consul_manager.service_healthcheck():
            node_fail = False
            address = node['Node']['Address']
            if not self._check_machine_exists(address):
                logging.error("check_machine: machine {} not found".format(address))
                continue
            service_instance = self.config['RPAAS_SERVICE_NAME']
            for tag in node['Service']['Tags']:
                if self.config['RPAAS_SERVICE_NAME'] in tag:
                    continue
                service_instance = tag
            for check in node['Checks']:
                if check['Status'] != 'passing':
                    node_fail = True
                    break
            task_name = "restore_{}".format(address)
            if node_fail:
                try:
                    self.task_manager.ensure_ready(task_name)
                    self.task_manager.create({"_id": task_name, "host": address,
                                              "instance": service_instance,
                                              "created": datetime.datetime.utcnow()})
                except:
                    pass
            else:
                try:
                    self.task_manager.remove(task_name)
                except:
                    pass

    def _check_machine_exists(self, address):
        machine_data = self.storage.find_host_id(address)
        if machine_data is None:
            return False
        return True


class DownloadCertTask(BaseManagerTask):

    def run(self, config, name, plugin, csr, key, domain):
        try:
            self.init_config(config)
            ssl.generate_crt(self.config, name, plugin, csr, key, domain)
        finally:
            self.storage.remove_task(name)


class RevokeCertTask(BaseManagerTask):

    def run(self, config, name, plugin, domain):
        try:
            self.init_config(config)
            lb = LoadBalancer.find(name, self.config)
            if lb is None:
                raise storage.InstanceNotFoundError()

            plugin_class = ssl_plugins.get(plugin)
            plugin_obj = plugin_class(domain, os.environ.get('RPAAS_PLUGIN_LE_EMAIL', 'admin@'+domain),
                                      name)
            plugin_obj.revoke()
            self.storage.remove_le_certificate(name, domain)
        except Exception, e:
            logging.error("Error in ssl plugin task: {}".format(e))
            raise e
        finally:
            self.storage.remove_task(name)


class RenewCertsTask(BaseManagerTask):

    def run(self, config):
        self.init_config(config)
        expires_in = int(self.config.get("LE_CERTIFICATE_EXPIRATION_DAYS", 90))
        limit = datetime.datetime.utcnow() - datetime.timedelta(days=expires_in - 3)
        query = {"created": {"$lte": limit}}
        for cert in self.storage.find_le_certificates(query):
            metadata = self.storage.find_instance_metadata(cert["name"])
            config = copy.deepcopy(self.config)
            if metadata and "plan_name" in metadata:
                plan = self.storage.find_plan(metadata["plan_name"])
                config.update(plan.config)
            self.renew(cert, config)

    def renew(self, cert, config):
        key = ssl.generate_key()
        csr = ssl.generate_csr(key, cert["domain"])
        DownloadCertTask().delay(config=config, name=cert["name"], plugin="le",
                                 csr=csr, key=key, domain=cert["domain"])
