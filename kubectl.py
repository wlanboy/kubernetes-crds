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

@dataclass
class NamespaceInfo:
    name: str
    labels: dict[str, str]


@dataclass
class CRDStat:
    name: str           # e.g. certificates.cert-manager.io
    group: str
    kind: str
    plural: str
    namespaced: bool
    instances_by_namespace: dict[str, int] = field(default_factory=dict)

    @property
    def total_instances(self) -> int:
        return sum(self.instances_by_namespace.values())

    @property
    def namespace_count(self) -> int:
        return sum(1 for v in self.instances_by_namespace.values() if v > 0)


# ---------------------------------------------------------------------------
# Namespace listing
# ---------------------------------------------------------------------------

def get_namespaces() -> list[NamespaceInfo]:
    v1 = client.CoreV1Api()
    ns_list = cast(V1NamespaceList, v1.list_namespace())
    return [
        NamespaceInfo(name=ns.metadata.name, labels=ns.metadata.labels or {})
        for ns in (ns_list.items or [])
    ]


# ---------------------------------------------------------------------------
# Shared helpers (used by CRD stats, adoption, and kubectl_istio)
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


def _count_custom(custom: client.CustomObjectsApi, group: str, versions: list[str],
                  namespace: str, plural: str) -> int:
    for version in versions:
        try:
            result = _custom_list(
                custom, group=group, version=version, namespace=namespace, plural=plural,
            )
            return len(result.get("items", []))
        except ApiException as e:
            if e.status == 404:
                continue
    return 0


def _count_instances_by_namespace(custom: client.CustomObjectsApi, *, group: str, version: str,
                                  plural: str, namespaces: list[str]) -> dict[str, int]:
    """Count group/version/plural instances across namespaces concurrently."""
    if not namespaces:
        return {}

    def _count(ns: str) -> tuple[str, int]:
        try:
            resp = _custom_list(custom, group=group, version=version, namespace=ns, plural=plural)
            return ns, len(resp.get("items", []))
        except ApiException:
            return ns, 0

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(namespaces))) as pool:
        results = pool.map(_count, namespaces)

    return {ns: count for ns, count in results if count}


# ---------------------------------------------------------------------------
# CRD statistics
# ---------------------------------------------------------------------------

def _storage_version(crd: Any) -> str:
    for v in crd.spec.versions:
        if getattr(v, "storage", False):
            return v.name  # type: ignore[no-any-return]
    return crd.spec.versions[0].name if crd.spec.versions else "v1"  # type: ignore[no-any-return]


def get_crd_stats(namespace_names: list[str]) -> list[CRDStat]:
    ext = client.ApiextensionsV1Api()
    custom = client.CustomObjectsApi()

    crd_list = cast(V1CustomResourceDefinitionList, ext.list_custom_resource_definition())
    stats: list[CRDStat] = []

    for crd in (crd_list.items or []):
        spec = crd.spec
        version = _storage_version(crd)
        stat = CRDStat(
            name=crd.metadata.name,
            group=spec.group,
            kind=spec.names.kind,
            plural=spec.names.plural,
            namespaced=spec.scope == "Namespaced",
        )

        if stat.namespaced:
            stat.instances_by_namespace = _count_instances_by_namespace(
                custom, group=spec.group, version=version,
                plural=spec.names.plural, namespaces=namespace_names,
            )
        else:
            try:
                result = _custom_list(
                    custom, group=spec.group, version=version,
                    namespace=None, plural=spec.names.plural,
                )
                count = len(result.get("items", []))
                if count:
                    stat.instances_by_namespace["(cluster)"] = count
            except ApiException:
                pass

        if stat.total_instances > 0:
            stats.append(stat)

    return sorted(stats, key=lambda s: s.total_instances, reverse=True)


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

    namespaces_to_scan = [namespace] if namespace is not None else [
        ns.name for ns in get_namespaces()
    ]

    crd_list = cast(V1CustomResourceDefinitionList, ext.list_custom_resource_definition())
    result: list[CRDVersionedInfo] = []

    for crd in (crd_list.items or []):
        spec = crd.spec
        is_namespaced = spec.scope == "Namespaced"

        if not is_namespaced and namespace is not None:
            continue

        status = getattr(crd, "status", None)
        info = CRDVersionedInfo(
            name=crd.metadata.name,
            group=spec.group,
            kind=spec.names.kind,
            plural=spec.names.plural,
            namespaced=is_namespaced,
            stored_versions=list(getattr(status, "stored_versions", None) or []),
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
                    except ApiException:
                        pass

            info.versions.append(vinfo)

        result.append(info)

    return sorted(result, key=lambda i: (i.group, i.kind))
