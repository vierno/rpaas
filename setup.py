# -*- coding: utf-8 -*-

# Copyright 2015 hm authors. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.
import codecs

from setuptools import setup, find_packages

README = codecs.open('README.rst', encoding='utf-8').read()

setup(
    name="tsuru-rpaas",
    version="0.1.0",
    description="Reverse proxy as-a-service API for Tsuru PaaS",
    long_description=README,
    author="Tsuru",
    author_email="tsuru@corp.globo.com",
    classifiers=[
        "Programming Language :: Python :: 2.7",
    ],
    packages=find_packages(exclude=["docs", "tests"]),
    include_package_data=True,
    install_requires=[
        "cryptography==1.1.2",
        "setuptools==18.4",
        "Flask==0.9",
        "requests==2.4.3",
        "gevent==1.1b6",
        "gunicorn==0.17.2",
        "tsuru-hm==0.5.4",
        "celery[redis]",
        "flower==0.7.3",
        "GloboNetworkAPI==0.2.2",
        "python-consul",
        "raven==4.2.3",
        "blinker==1.4",
        "acme==0.0.0.dev20151108",
        "letsencrypt==0.0.0.dev20151108",
    ],
    extras_require={
        'tests': [
            "mock==1.0.1",
            "flake8==2.1.0",
            "coverage==3.7.1",
            "freezegun==0.2.8",
        ]
    },
)
