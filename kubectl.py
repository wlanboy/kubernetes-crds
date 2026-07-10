"""Kubernetes data collection — core: namespaces, CRDs, adoption."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, cast

import urllib3
from kubernetes import client, config
from kubernetes.client import (
    V1CustomResourceDefinitionList,
    V1NamespaceList,
)
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

# Max concurrent API calls when fanning a single group/version out across namespaces.
_MAX_WORKERS = 10


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def load_config(*, verify_ssl: bool = True) -> None:
    """Load kubeconfig (in-cluster first, then local ~/.kube/config).

    If ``verify_ssl`` is False, TLS certificate verification is disabled for
    all subsequent API calls (equivalent to ``kubectl``/``oc``
    ``--insecure-skip-tls-verify``) — useful against clusters with
    self-signed certificates.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    if not verify_ssl:
        cfg = client.Configuration.get_default_copy()
        cfg.verify_ssl = False
        client.Configuration.set_default(cfg)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Namespace listing
# ---------------------------------------------------------------------------

def get_namespaces() -> list[str]:
    v1 = client.CoreV1Api()
    ns_list = cast(V1NamespaceList, v1.list_namespace())
    return [ns.metadata.name for ns in (ns_list.items or [])]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _custom_list(custom: client.CustomObjectsApi, *, group: str, version: str,
                 namespace: str | None, plural: str) -> dict[str, Any]:
    if namespace is not None:
        result = custom.list_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural,
        )
    else:
        result = custom.list_cluster_custom_object(
            group=group, version=version, plural=plural,
        )
    return cast(dict[str, Any], result)


def _count_instances_by_namespace(custom: client.CustomObjectsApi, *, group: str, version: str,
                                  plural: str, namespaces: list[str]) -> dict[str, int]:
    """Count group/version/plural instances across namespaces concurrently."""
    if not namespaces:
        return {}

    def _count(ns: str) -> tuple[str, int]:
        try:
            resp = _custom_list(custom, group=group, version=version, namespace=ns, plural=plural)
            return ns, len(resp.get("items", []))
        except ApiException as e:
            logger.debug(
                "Failed to list %s/%s %s in namespace %s: %s", group, version, plural, ns, e.reason,
            )
            return ns, 0

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(namespaces))) as pool:
        results = pool.map(_count, namespaces)

    return {ns: count for ns, count in results if count}


# ---------------------------------------------------------------------------
# CRD listing across all versions
# ---------------------------------------------------------------------------

@dataclass
class CRDVersionInfo:
    version: str
    served: bool
    storage: bool
    deprecated: bool = False
    deprecation_warning: str | None = None
    instances_by_namespace: dict[str, int] = field(default_factory=dict)

    @property
    def total_instances(self) -> int:
        return sum(self.instances_by_namespace.values())


@dataclass
class CRDVersionedInfo:
    name: str           # e.g. certificates.cert-manager.io
    group: str
    kind: str
    plural: str
    namespaced: bool
    versions: list[CRDVersionInfo] = field(default_factory=list)
    # Versions the API server still has objects persisted as (CRD status.storedVersions).
    stored_versions: list[str] = field(default_factory=list)
    # spec.conversion.strategy: "None" or "Webhook". Webhook conversion means
    # reading/writing non-storage versions depends on an external webhook being
    # reachable — worth flagging separately from the per-version served/storage flags.
    conversion_strategy: str = "None"
    # status.conditions[type=Established/NamesAccepted]. A CRD stuck at False here
    # never became usable (e.g. a names conflict) — it shows up in list_custom_resource_definition()
    # like any other CRD, but every API call against it will fail.
    established: bool = True
    names_accepted: bool = True
    established_message: str | None = None
    names_accepted_message: str | None = None

    @property
    def total_instances(self) -> int:
        return sum(v.total_instances for v in self.versions)

    @property
    def storage_version(self) -> str | None:
        return next((v.version for v in self.versions if v.storage), None)

    @property
    def pending_migration_versions(self) -> list[str]:
        """Stored versions other than the current storage version — objects still
        persisted under these have not been migrated and block their removal."""
        current = self.storage_version
        return [v for v in self.stored_versions if v != current]


def get_crd_versions(namespace: str | None = None) -> list[CRDVersionedInfo]:
    """List every CRD together with all of its API versions and, per served
    version, the instance count broken down by namespace.

    If ``namespace`` is given, only namespaced CRDs are inspected and only
    instances in that namespace are counted (cluster-scoped CRDs have no
    per-namespace instances and are skipped). If ``namespace`` is ``None``,
    every namespace in the cluster is scanned and cluster-scoped CRDs are
    included under the pseudo-namespace ``"(cluster)"``.
    """
    ext = client.ApiextensionsV1Api()
    custom = client.CustomObjectsApi()

    namespaces_to_scan = [namespace] if namespace is not None else get_namespaces()

    crd_list = cast(V1CustomResourceDefinitionList, ext.list_custom_resource_definition())
    result: list[CRDVersionedInfo] = []

    for crd in (crd_list.items or []):
        spec = crd.spec
        is_namespaced = spec.scope == "Namespaced"

        if not is_namespaced and namespace is not None:
            continue

        status = getattr(crd, "status", None)
        conversion = getattr(spec, "conversion", None)
        conditions = {c.type: c for c in (getattr(status, "conditions", None) or [])}
        established_cond = conditions.get("Established")
        names_accepted_cond = conditions.get("NamesAccepted")
        info = CRDVersionedInfo(
            name=crd.metadata.name,
            group=spec.group,
            kind=spec.names.kind,
            plural=spec.names.plural,
            namespaced=is_namespaced,
            stored_versions=list(getattr(status, "stored_versions", None) or []),
            conversion_strategy=getattr(conversion, "strategy", None) or "None",
            established=established_cond is None or established_cond.status == "True",
            names_accepted=names_accepted_cond is None or names_accepted_cond.status == "True",
            established_message=established_cond.message
            if established_cond is not None and established_cond.status != "True" else None,
            names_accepted_message=names_accepted_cond.message
            if names_accepted_cond is not None and names_accepted_cond.status != "True" else None,
        )

        for v in (spec.versions or []):
            vinfo = CRDVersionInfo(
                version=v.name, served=v.served, storage=v.storage,
                deprecated=bool(getattr(v, "deprecated", False)),
                deprecation_warning=getattr(v, "deprecation_warning", None),
            )

            if v.served:
                if is_namespaced:
                    vinfo.instances_by_namespace = _count_instances_by_namespace(
                        custom, group=spec.group, version=v.name,
                        plural=spec.names.plural, namespaces=namespaces_to_scan,
                    )
                else:
                    try:
                        resp = _custom_list(
                            custom, group=spec.group, version=v.name,
                            namespace=None, plural=spec.names.plural,
                        )
                        count = len(resp.get("items", []))
                        if count:
                            vinfo.instances_by_namespace["(cluster)"] = count
                    except ApiException as e:
                        logger.debug(
                            "Failed to list %s/%s %s (cluster-scoped): %s",
                            spec.group, v.name, spec.names.plural, e.reason,
                        )

            info.versions.append(vinfo)

        result.append(info)

    return sorted(result, key=lambda i: (i.group, i.kind))
