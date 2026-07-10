"""OpenShift-specific data collection — built-in aggregated APIs (Route, BuildConfig,
DeploymentConfig, ...) that live in *.openshift.io API groups rather than as
CustomResourceDefinitions, so they never show up in list_custom_resource_definition()."""
from __future__ import annotations

import logging
from typing import Any, cast

import urllib3
from kubernetes import client
from kubernetes.client import V1APIGroupList
from kubernetes.client.rest import ApiException

from kubectl import (
    _REQUEST_TIMEOUT,
    CRDVersionedInfo,
    CRDVersionInfo,
    _count_instances_by_namespace,
    _custom_list,
    _error_reason,
    get_namespaces,
)

logger = logging.getLogger(__name__)

_OPENSHIFT_GROUP_SUFFIX = ".openshift.io"


def _list_openshift_group_versions() -> list[tuple[str, str]]:
    """Return (group, preferred_version) for every installed *.openshift.io API group."""
    apis_api = client.ApisApi()
    group_list = cast(
        V1APIGroupList, apis_api.get_api_versions(_request_timeout=_REQUEST_TIMEOUT),
    )
    return [
        (group.name, group.preferred_version.version)
        for group in (group_list.groups or [])
        if group.name.endswith(_OPENSHIFT_GROUP_SUFFIX)
    ]


def _discover_group_version_resources(api_client: client.ApiClient, group: str,
                                       version: str) -> list[dict[str, Any]]:
    """Raw discovery call for /apis/{group}/{version} — the resource list (Kind, plural
    name, namespaced) isn't modeled by the generated client since it's cluster-specific."""
    try:
        resp = cast(
            "tuple[dict[str, Any], int, dict[str, str]]",
            api_client.call_api(
                f"/apis/{group}/{version}", "GET",
                auth_settings=["BearerToken"], response_types_map={200: "object"},
                _request_timeout=_REQUEST_TIMEOUT,
            ),
        )
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        logger.debug("Discovery failed for %s/%s: %s", group, version, e)
        return []
    body = resp[0]
    if body is None:
        return []
    # Skip subresources such as "routes/status", and any entry missing the
    # fields we rely on downstream (name, kind are required by the
    # APIResourceList schema but discovery responses are cluster-supplied).
    return [
        r for r in body.get("resources", [])
        if r.get("name") and "/" not in r["name"] and r.get("kind")
    ]


def get_openshift_resource_versions(namespace: str | None = None) -> list[CRDVersionedInfo]:
    """Like kubectl.get_crd_versions, but for built-in OpenShift API resources."""
    custom = client.CustomObjectsApi()
    api_client = client.ApiClient()

    namespaces_to_scan = [namespace] if namespace is not None else get_namespaces()

    result: list[CRDVersionedInfo] = []

    for group, version in _list_openshift_group_versions():
        for res in _discover_group_version_resources(api_client, group, version):
            is_namespaced = bool(res.get("namespaced", False))

            if not is_namespaced and namespace is not None:
                continue

            plural = res["name"]
            info = CRDVersionedInfo(
                name=f"{plural}.{group}",
                group=group,
                kind=res["kind"],
                plural=plural,
                namespaced=is_namespaced,
            )
            vinfo = CRDVersionInfo(version=version, served=True, storage=True)

            if is_namespaced:
                vinfo.instances_by_namespace, vinfo.fetch_errors = _count_instances_by_namespace(
                    custom, group=group, version=version,
                    plural=plural, namespaces=namespaces_to_scan,
                )
            else:
                try:
                    resp = _custom_list(
                        custom, group=group, version=version, namespace=None, plural=plural,
                    )
                    count = len(resp.get("items", []))
                    if count:
                        vinfo.instances_by_namespace["(cluster)"] = count
                except (ApiException, urllib3.exceptions.HTTPError) as e:
                    logger.debug(
                        "Failed to list %s/%s %s (cluster-scoped): %s", group, version, plural, e,
                    )
                    vinfo.fetch_errors["(cluster)"] = _error_reason(e)

            info.versions.append(vinfo)
            result.append(info)

    return sorted(result, key=lambda i: (i.group, i.kind))
