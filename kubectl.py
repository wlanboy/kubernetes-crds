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

# Per-request timeout (seconds) for every Kubernetes API call. Without this, a
# misbehaving apiserver (e.g. a CRD with a broken conversion webhook — the
# apiserver's watch-cache reflector for that resource can hang indefinitely
# trying to reach it, even for unrelated namespaces/versions) blocks the
# underlying HTTP call forever: the generated client only wraps SSL errors
# into ApiException, so a read timeout otherwise never surfaces at all.
_REQUEST_TIMEOUT = 30


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

    # Retrying a request that failed because the apiserver itself is stuck
    # (e.g. its watch-cache reflector is wedged behind a broken CRD conversion
    # webhook) just multiplies _REQUEST_TIMEOUT by the retry count instead of
    # helping — one attempt per call is enough given callers already treat a
    # timeout as "no data available" and move on.
    cfg = client.Configuration.get_default_copy()
    cfg.retries = 0
    if not verify_ssl:
        cfg.verify_ssl = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    client.Configuration.set_default(cfg)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Namespace listing
# ---------------------------------------------------------------------------

def get_namespaces() -> list[str]:
    v1 = client.CoreV1Api()
    ns_list = cast(V1NamespaceList, v1.list_namespace(_request_timeout=_REQUEST_TIMEOUT))
    return [ns.metadata.name for ns in (ns_list.items or [])]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _custom_list(custom: client.CustomObjectsApi, *, group: str, version: str,
                 namespace: str | None, plural: str) -> dict[str, Any]:
    if namespace is not None:
        result = custom.list_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural,
            _request_timeout=_REQUEST_TIMEOUT,
        )
    else:
        result = custom.list_cluster_custom_object(
            group=group, version=version, plural=plural,
            _request_timeout=_REQUEST_TIMEOUT,
        )
    return cast(dict[str, Any], result)


def _count_instances_by_namespace(
    custom: client.CustomObjectsApi, *, group: str, version: str,
    plural: str, namespaces: list[str],
) -> tuple[dict[str, int], dict[str, str]]:
    """Count group/version/plural instances across namespaces concurrently.

    Returns (counts, errors): ``errors`` maps namespace -> failure reason for
    namespaces where the count could not be determined at all, so callers can
    tell "confirmed zero instances" apart from "instance count unknown".
    """
    if not namespaces:
        return {}, {}

    def _count(ns: str) -> tuple[str, int | None, str | None]:
        try:
            resp = _custom_list(custom, group=group, version=version, namespace=ns, plural=plural)
            return ns, len(resp.get("items", [])), None
        except (ApiException, urllib3.exceptions.HTTPError) as e:
            logger.debug(
                "Failed to list %s/%s %s in namespace %s: %s", group, version, plural, ns, e,
            )
            return ns, None, _error_reason(e)

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(namespaces))) as pool:
        results = list(pool.map(_count, namespaces))

    counts = {ns: count for ns, count, _ in results if count}
    errors = {ns: error for ns, _, error in results if error is not None}
    return counts, errors


def _error_reason(e: Exception) -> str:
    """Short, human-readable description of an API failure. ApiException.reason
    is a concise HTTP reason phrase ("Forbidden", "Too Many Requests"); str(e)
    on the exception itself would dump the full response headers and body."""
    return getattr(e, "reason", None) or str(e)


def _describe_conversion_webhook(conversion: Any) -> tuple[str | None, bool]:
    """Return (target, ca_bundle_present) describing where a Webhook conversion
    strategy sends conversion requests — either a cluster-internal Service
    (name.namespace:port/path) or an external URL."""
    webhook = getattr(conversion, "webhook", None)
    client_config = getattr(webhook, "client_config", None)
    if client_config is None:
        return None, False

    service = getattr(client_config, "service", None)
    if service is not None:
        port = service.port or 443
        path = service.path or ""
        target = f"{service.name}.{service.namespace}:{port}{path}"
    else:
        target = getattr(client_config, "url", None)

    return target, bool(getattr(client_config, "ca_bundle", None))


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
    # Namespace (or "(cluster)" for cluster-scoped resources) -> failure reason,
    # for namespaces where the instance count could not be fetched at all (e.g.
    # apiserver timeout). Distinct from a namespace simply having zero instances.
    fetch_errors: dict[str, str] = field(default_factory=dict)

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
    # spec.conversion.webhook.clientConfig — where the conversion webhook is reached,
    # either a cluster-internal Service or an external URL. Only meaningful when
    # conversion_strategy is "Webhook"; not verified for actual reachability.
    conversion_webhook_target: str | None = None
    conversion_webhook_ca_bundle_present: bool = False
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

    crd_list = cast(
        V1CustomResourceDefinitionList,
        ext.list_custom_resource_definition(_request_timeout=_REQUEST_TIMEOUT),
    )
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
        webhook_target, webhook_ca_bundle_present = _describe_conversion_webhook(conversion)
        info = CRDVersionedInfo(
            name=crd.metadata.name,
            group=spec.group,
            kind=spec.names.kind,
            plural=spec.names.plural,
            namespaced=is_namespaced,
            stored_versions=list(getattr(status, "stored_versions", None) or []),
            conversion_strategy=getattr(conversion, "strategy", None) or "None",
            conversion_webhook_target=webhook_target,
            conversion_webhook_ca_bundle_present=webhook_ca_bundle_present,
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
                    vinfo.instances_by_namespace, vinfo.fetch_errors = _count_instances_by_namespace(
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
                    except (ApiException, urllib3.exceptions.HTTPError) as e:
                        logger.debug(
                            "Failed to list %s/%s %s (cluster-scoped): %s",
                            spec.group, v.name, spec.names.plural, e,
                        )
                        vinfo.fetch_errors["(cluster)"] = _error_reason(e)

            info.versions.append(vinfo)

        result.append(info)

    return sorted(result, key=lambda i: (i.group, i.kind))
