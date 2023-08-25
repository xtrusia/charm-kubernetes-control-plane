#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Charm."""

import logging
import os
import socket

import auth_webhook
import charms.contextual_status as status
import leader_data
import ops
from charms import kubernetes_snaps
from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from charms.reconciler import Reconciler
from ops import BlockedStatus, WaitingStatus
from ops.interface_tls_certificates import CertificatesRequires

log = logging.getLogger(__name__)


class KubernetesControlPlaneCharm(ops.CharmBase):
    """Charm."""

    def __init__(self, *args):
        super().__init__(*args)
        self.certificates = CertificatesRequires(self, endpoint="certificates")
        self.etcd = EtcdReactiveRequires(self)
        self.reconciler = Reconciler(self, self.reconcile)

    def configure_apiserver(self):
        if not self.etcd.is_ready:
            status.add(WaitingStatus("Waiting for etcd"))
            return

        kubernetes_snaps.configure_apiserver(
            advertise_address=self.model.get_binding("kube-control")
            .network.ingress_addresses[0]
            .exploded,
            audit_policy=self.model.config["audit-policy"],
            audit_webhook_conf=self.model.config["audit-webhook-config"],
            auth_webhook_conf=auth_webhook.auth_webhook_conf,
            authorization_mode=self.model.config["authorization-mode"],
            # TODO: cluster_cidr from CNI relation
            cluster_cidr=None,
            etcd_connection_string=self.etcd.get_connection_string(),
            extra_args_config=self.model.config["api-extra-args"],
            privileged=self.model.config["allow-privileged"],
            service_cidr=self.model.config["service-cidr"],
        )

    def configure_auth_webhook(self):
        auth_webhook.configure(
            charm_dir=self.charm_dir,
            custom_authn_endpoint=self.model.config["authn-webhook-endpoint"],
            # TODO: aws iam, keystone
            # aws_iam_endpoint=???,
            # keystone_endpoint=???
        )

    def configure_controller_manager(self):
        cluster_name = self.get_cluster_name()
        if not cluster_name:
            status.add(WaitingStatus("Waiting for cluster name"))
            return

        kubernetes_snaps.configure_controller_manager(
            # TODO: cluster_cidr from CNI relation
            cluster_cidr=None,
            cluster_name=cluster_name,
            extra_args_config=self.model.config["controller-manager-extra-args"],
            kubeconfig="/root/cdk/kubecontrollermanagerconfig",
            service_cidr=self.model.config["service-cidr"],
        )

    def configure_scheduler(self):
        kubernetes_snaps.configure_scheduler(
            extra_args_config=self.model.config["scheduler-extra-args"],
            kubeconfig="/root/cdk/kubeschedulerconfig",
        )

    def create_kubeconfigs(self):
        ca = self.certificates.ca
        if not ca:
            status.add(WaitingStatus("Waiting for certificates"))
            return

        if not self.etcd.is_ready:
            status.add(WaitingStatus("Waiting for etcd"))
            return

        local_server = "https://127.0.0.1:6443"
        node_name = kubernetes_snaps.get_node_name()
        # TODO: support loadbalancers, hacluster, etc
        public_address = kubernetes_snaps.get_public_address()
        public_server = f"https://{public_address}:6443"

        if not os.path.exists("/root/.kube/config"):
            # Create a bootstrap client config. This initial config will allow
            # us to get and create auth webhook tokens via the Kubernetes API,
            # but will not have the final admin token just yet.
            kubernetes_snaps.create_kubeconfig(
                "/root/.kube/config",
                ca=ca,
                server=local_server,
                user="admin",
                token=auth_webhook.token_generator(),
            )

        admin_token = auth_webhook.create_token(
            uid="admin",
            username="admin",
            # wokeignore:rule=master
            groups=["system:masters"],
        )

        for dest in ["/root/.kube/config", "/home/ubuntu/.kube/config"]:
            kubernetes_snaps.create_kubeconfig(
                dest,
                ca=ca,
                server=local_server,
                token=admin_token,
                user="admin",
            )

        kubernetes_snaps.create_kubeconfig(
            "/home/ubuntu/config",
            ca=ca,
            server=public_server,
            token=admin_token,
            user="admin",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubecontrollermanagerconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid="kube-controller-manager", username="system:kube-controller-manager", groups=[]
            ),
            user="kube-controller-manager",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubeschedulerconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid="system:kube-scheduler", username="system:kube-scheduler", groups=[]
            ),
            user="kube-scheduler",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubeconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid=self.unit.name,
                username=f"system:node:{node_name.lower()}",
                groups=["system:nodes"],
            ),
            user="kubelet",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubeproxyconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid="kube-proxy", username="system:kube-proxy", groups=[]
            ),
            user="kube-proxy",
        )

    def get_cluster_name(self):
        peer_relation = self.model.get_relation("peer")
        cluster_name = peer_relation.data[self.app].get("cluster-name")

        if cluster_name:
            return cluster_name

        if not self.unit.is_leader():
            status.add(WaitingStatus("Waiting for cluster name from leader"))
            return None

        # Check for old cluster name in leader data
        cluster_name = leader_data.get("cluster_tag")
        if cluster_name:
            peer_relation.data[self.app]["cluster-name"] = cluster_name
            leader_data.set("cluster_tag", "")
            return cluster_name

        cluster_name = f"kubernetes-{auth_webhook.token_generator().lower()}"
        peer_relation.data[self.app]["cluster-name"] = cluster_name
        return cluster_name

    def reconcile(self, event):
        """Reconcile state change events."""
        kubernetes_snaps.install(channel=self.model.config["channel"], control_plane=True)
        kubernetes_snaps.configure_services_restart_always(control_plane=True)
        self.request_certificates()
        self.write_certificates()
        self.write_etcd_client_credentials()
        self.write_service_account_key()
        self.configure_auth_webhook()
        self.configure_apiserver()
        self.create_kubeconfigs()
        self.configure_controller_manager()
        self.configure_scheduler()

    def request_certificates(self):
        """Request client and server certificates."""
        if not self.certificates.relation:
            status.add(BlockedStatus("Missing relation to certificate authority"))
            return

        bind_addrs = kubernetes_snaps.get_bind_addresses()
        common_name = kubernetes_snaps.get_public_address()
        domain = self.config["dns_domain"]
        extra_sans = self.config["extra_sans"].split()
        k8s_service_addrs = kubernetes_snaps.get_kubernetes_service_addresses(
            self.config["service-cidr"].split(",")
        )
        ingress_addrs = [
            # RFC 5280 section 4.2.1.6: "For IP version 6 ... the octet string
            # MUST contain exactly sixteen octets." We'll use .exploded to be
            # safe.
            addr.exploded
            for addr in self.model.get_binding("kube-control").network.ingress_addresses
        ]

        sans = [
            # The CN field is checked as a hostname, so if it's an IP, it
            # won't match unless also included in the SANs as an IP field.
            common_name,
            "127.0.0.1",
            socket.gethostname(),
            socket.getfqdn(),
            "kubernetes",
            f"kubernetes.{domain}",
            "kubernetes.default",
            "kubernetes.default.svc",
            f"kubernetes.default.svc.{domain}",
        ]
        sans += bind_addrs
        sans += ingress_addrs
        sans += k8s_service_addrs
        sans += extra_sans
        sans = list(set(sans))

        self.certificates.request_client_cert("system:kube-apiserver")
        self.certificates.request_server_cert(cn=common_name, sans=sans)

    def write_service_account_key(self):
        peer_relation = self.model.get_relation("peer")
        key = peer_relation.data[self.app].get("service-account-key")

        if key:
            kubernetes_snaps.write_service_account_key(key)
            return

        if not self.unit.is_leader():
            status.add(WaitingStatus("Waiting for key from leader"))
            return

        # Check for old key in leader data
        key = leader_data.get("/root/cdk/serviceaccount.key")
        if key:
            peer_relation.data[self.app]["service-account-key"] = key
            leader_data.set("/root/cdk/serviceaccount.key", "")
            return

        key = kubernetes_snaps.create_service_account_key()
        peer_relation.data[self.app]["service-account-key"] = key

    def write_certificates(self):
        """Write certificates from the certificates relation."""
        common_name = kubernetes_snaps.get_public_address()
        ca = self.certificates.ca
        client_cert = self.certificates.client_certs_map.get("system:kube-apiserver")
        server_cert = self.certificates.server_certs_map.get(common_name)

        if not ca or not client_cert or not server_cert:
            status.add(WaitingStatus("Waiting for certificates"))
            return

        kubernetes_snaps.write_certificates(
            ca=ca,
            client_cert=client_cert.cert,
            client_key=client_cert.key,
            server_cert=server_cert.cert,
            server_key=server_cert.key,
        )

    def write_etcd_client_credentials(self):
        """Write etcd client credentials from the etcd relation."""
        if not self.etcd.relation:
            status.add(BlockedStatus("Missing relation to etcd"))
            return

        if not self.etcd.is_ready:
            status.add(WaitingStatus("Waiting for etcd"))
            return

        creds = self.etcd.get_client_credentials()

        kubernetes_snaps.write_etcd_client_credentials(
            ca=creds["client_ca"], cert=creds["client_cert"], key=creds["client_key"]
        )


if __name__ == "__main__":  # pragma: nocover
    ops.main(KubernetesControlPlaneCharm)
