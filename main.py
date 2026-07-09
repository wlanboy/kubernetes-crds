"""CLI: list Kubernetes CRDs with all versions and instance counts per namespace."""
from __future__ import annotations

import argparse
import sys

from kubectl import get_crd_versions, load_config
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
    args = parser.parse_args()

    load_config(verify_ssl=not args.insecure_skip_tls_verify)
    crds = get_crd_versions(namespace=args.namespace)

    if args.openshift:
        crds = sorted(
            crds + get_openshift_resource_versions(namespace=args.namespace),
            key=lambda c: (c.group, c.kind),
        )

    if not crds:
        print("No CRDs found.")
        return 0

    if args.unused:
        unused_crds = [crd for crd in crds if crd.total_instances == 0]
        if not unused_crds:
            print("No unused CRDs found.")
            return 0

        rows = [
            (crd.name, crd.group, crd.kind, "Namespaced" if crd.namespaced else "Cluster")
            for crd in unused_crds
        ]
        headers = ("CRD", "GROUP", "KIND", "SCOPE")
        print(_format_table(rows, headers))
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
                v.version,
                "yes" if v.served else "no",
                "yes" if v.storage else "no",
            )
            if v.instances_by_namespace:
                for ns, count in sorted(v.instances_by_namespace.items()):
                    row = (*base, ns, str(count)) if show_namespace_column else (*base, str(count))
                    rows.append(row)
            else:
                row = (*base, "-", "-") if show_namespace_column else (*base, "-")
                rows.append(row)

    headers = ("CRD", "GROUP", "KIND", "SCOPE", "VERSION", "SERVED", "STORAGE")
    if show_namespace_column:
        headers += ("NAMESPACE",)
    headers += ("INSTANCES",)
    print(_format_table(rows, headers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
