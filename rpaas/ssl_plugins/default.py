# -*- coding: utf-8 -*-

# Copyright 2015 rpaas authors. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

import datetime
import uuid

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes

from rpaas.ssl_plugins import BaseSSLPlugin


class Default(BaseSSLPlugin):
    ''' Generate self-signed certificate
    '''

    def __init__(self, domain):
        self.domain = domain

    def upload_csr(self, csr):
        pass

    def download_crt(self, key=None):
        one_day = datetime.timedelta(1, 0, 0)

        private_key = serialization.load_pem_private_key(
            key,
            password=None,
            backend=default_backend()
        )

        public_key = private_key.public_key()

        builder = x509.CertificateBuilder()
        builder = builder.subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, self.domain),
        ]))
        builder = builder.issuer_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, self.domain),
        ]))
        builder = builder.not_valid_before(datetime.datetime.today() - one_day)
        builder = builder.not_valid_after(datetime.datetime(2018, 8, 2))
        builder = builder.serial_number(int(uuid.uuid4()))
        builder = builder.public_key(public_key)
        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True,
        )
        certificate = builder.sign(
            private_key=private_key, algorithm=hashes.SHA256(),
            backend=default_backend()
        )

        return certificate.public_bytes(serialization.Encoding.PEM)

    def revoke(self):
        pass
