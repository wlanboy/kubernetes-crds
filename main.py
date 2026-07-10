"""CLI: list Kubernetes CRDs with all versions and instance counts per namespace."""
from __future__ import annotations

import argparse
import logging
import sys

import urllib3
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

from kubectl import CRDVersionedInfo, get_crd_versions, load_config
from oc import get_openshift_resource_versions


def _format_table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt_row(headers), fmt_row(tuple("-" * w for w in widths))]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def _print_deprecation_warnings(crds: list[CRDVersionedInfo]) -> None:
    lines = [
        f"  {crd.name} {v.version}: {v.deprecation_warning or 'no deprecation message provided'}"
        for crd in crds
        for v in crd.versions
        if v.deprecated
    ]
    if lines:
        print("\nDeprecated API versions:")
        print("\n".join(lines))


def _print_webhook_conversion_targets(crds: list[CRDVersionedInfo]) -> None:
    lines = []
    for crd in crds:
        if crd.conversion_strategy != "Webhook":
            continue
        target = crd.conversion_webhook_target or "no clientConfig configured"
        ca_note = "" if crd.conversion_webhook_ca_bundle_present else " (no caBundle configured)"
        lines.append(f"  {crd.name}: {target}{ca_note}")
    if lines:
        print("\nWebhook conversion targets (reachability not verified):")
        print("\n".join(lines))


def _print_unhealthy_crds(crds: list[CRDVersionedInfo]) -> None:
    lines = []
    for crd in crds:
        if not crd.established:
            lines.append(
                f"  {crd.name}: not Established "
                f"({crd.established_message or 'no message provided'})",
            )
        if not crd.names_accepted:
            lines.append(
                f"  {crd.name}: NamesAccepted=False "
                f"({crd.names_accepted_message or 'no message provided'})",
            )
    if lines:
        print("\nUnhealthy CRDs (status conditions):")
        print("\n".join(lines))


def _print_migration_candidates(crds: list[CRDVersionedInfo]) -> None:
    lines = [
        f"  {crd.name}: instances still stored as {crd.pending_migration_versions} "
        f"(current storage version: {crd.storage_version})"
        for crd in crds
        if crd.pending_migration_versions
    ]
    if lines:
        print("\nStorage version migration candidates (status.storedVersions not yet cleaned up):")
        print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List all CRDs in a Kubernetes cluster with every API version "
                    "and, per version, the instance count per namespace.",
    )
    parser.add_argument(
        "-n", "--namespace",
        default=None,
        help="Only inspect this namespace (default: scan all namespaces; "
             "cluster-scoped CRDs are only shown when this is omitted).",
    )
    parser.add_argument(
        "--unused",
        action="store_true",
        help="Only list CRDs that have no instances in any version/namespace.",
    )
    parser.add_argument(
        "--openshift",
        action="store_true",
        help="Also include built-in OpenShift API resources (Route, BuildConfig, "
             "DeploymentConfig, ...) that are aggregated APIs rather than CRDs.",
    )
    parser.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="Disable TLS certificate verification against the API server "
             "(equivalent to kubectl/oc --insecure-skip-tls-verify).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging, e.g. for API calls that were skipped due to errors.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )

    try:
        load_config(verify_ssl=not args.insecure_skip_tls_verify)
    except ConfigException as e:
        print(f"Error: could not load Kubernetes configuration: {e}", file=sys.stderr)
        return 1

    try:
        crds = get_crd_versions(namespace=args.namespace)
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        print(f"Error: could not reach the Kubernetes API server: {e}", file=sys.stderr)
        return 1

    if args.openshift:
        try:
            crds = sorted(
                crds + get_openshift_resource_versions(namespace=args.namespace),
                key=lambda c: (c.group, c.kind),
            )
        except (ApiException, urllib3.exceptions.HTTPError) as e:
            print(
                f"Warning: could not fetch OpenShift resources, showing CRDs only: {e}",
                file=sys.stderr,
            )

    if not crds:
        print("No CRDs found.")
        return 0

    if args.unused:
        unused_crds = [crd for crd in crds if crd.total_instances == 0]
        if not unused_crds:
            print("No unused CRDs found.")
        else:
            rows = [
                (crd.name, crd.group, crd.kind, "Namespaced" if crd.namespaced else "Cluster")
                for crd in unused_crds
            ]
            headers = ("CRD", "GROUP", "KIND", "SCOPE")
            print(_format_table(rows, headers))

        _print_deprecation_warnings(crds)
        _print_migration_candidates(crds)
        _print_webhook_conversion_targets(crds)
        _print_unhealthy_crds(crds)
        return 0

    show_namespace_column = args.namespace is None

    rows: list[tuple[str, ...]] = []
    for crd in crds:
        for v in crd.versions:
            base = (
                crd.name,
                crd.group,
                crd.kind,
                "Namespaced" if crd.namespaced else "Cluster",
                crd.conversion_strategy,
                v.version,
                "yes" if v.served else "no",
                "yes" if v.storage else "no",
                "yes" if v.deprecated else "no",
            )
            if v.instances_by_namespace:
                for ns, count in sorted(v.instances_by_namespace.items()):
                    row = (*base, ns, str(count)) if show_namespace_column else (*base, str(count))
                    rows.append(row)
            else:
                row = (*base, "-", "-") if show_namespace_column else (*base, "-")
                rows.append(row)

    headers = ("CRD", "GROUP", "KIND", "SCOPE", "CONVERSION", "VERSION", "SERVED", "STORAGE", "DEPRECATED")
    if show_namespace_column:
        headers += ("NAMESPACE",)
    headers += ("INSTANCES",)
    print(_format_table(rows, headers))
    _print_deprecation_warnings(crds)
    _print_migration_candidates(crds)
    _print_webhook_conversion_targets(crds)
    _print_unhealthy_crds(crds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
