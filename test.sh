#!/usr/bin/env bash
# test.sh — Legt in einem kind-Cluster ein paar realitätsnahe CRDs
# (Istio, ArgoCD, cert-manager) inkl. Instanzen an, damit main.py etwas
# zum Auswerten hat. Enthält bewusst:
#   - deprecated API-Versionen (spec.versions[].deprecated + deprecationWarning)
#   - eine CRD ganz ohne Instanzen (Kandidat für --unused)
#   - eine CRD mit veralteter status.storedVersions-Version (Storage-Migration)
#   - eine cluster-scoped CRD
#   - eine CRD mit Webhook-Konversionsstrategie ohne caBundle und ohne
#     erreichbaren Webhook-Service (CONVERSION-Spalte + Abschnitt
#     "Webhook conversion targets"; nicht-Storage-Versionen lassen sich wegen
#     des fehlenden Webhooks nicht auflisten — main.py fängt das ab)
#   - zwei CRDs mit Namenskonflikt (gleiches Kind, gleiche Gruppe), damit die
#     zweite mit status.conditions[NamesAccepted]=False hängen bleibt
#     (Abschnitt "Unhealthy CRDs (status conditions)")
#
# Usage:
#   ./test.sh              # CRDs + Instanzen anlegen (Context: aktueller kubectl-Context)
#   ./test.sh <context>    # expliziten Context verwenden
#   ./test.sh cleanup      # alles wieder entfernen

set -euo pipefail

CONTEXT="${1:-}"

if [[ "$CONTEXT" == "cleanup" ]]; then
    echo "==> Räume Test-CRDs und Namespaces auf"
    kubectl delete crd \
        virtualservices.networking.istio.io \
        destinationrules.networking.istio.io \
        applications.argoproj.io \
        appprojects.argoproj.io \
        certificates.cert-manager.io \
        clusterissuers.cert-manager.io \
        backends.routing.example.io \
        endpointsets.routing.example.io \
        --ignore-not-found
    kubectl delete ns istio-system argocd cert-manager --ignore-not-found
    echo "==> Fertig."
    exit 0
fi

if [[ -n "$CONTEXT" ]]; then
    kubectl config use-context "$CONTEXT"
fi

echo "==> Aktueller Context: $(kubectl config current-context)"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "==> Namespaces anlegen"
for ns in istio-system argocd cert-manager; do
    kubectl create ns "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

wait_established() {
    local crd="$1"
    kubectl wait --for=condition=Established "crd/${crd}" --timeout=30s >/dev/null
}

# ---------------------------------------------------------------------------
# Istio: VirtualService — deprecated v1alpha3 (noch in Benutzung) + storage v1beta1
# ---------------------------------------------------------------------------
echo "==> CRD: virtualservices.networking.istio.io (deprecated v1alpha3 + v1beta1)"
cat > "$WORKDIR/vs-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: virtualservices.networking.istio.io
spec:
  group: networking.istio.io
  scope: Namespaced
  names:
    kind: VirtualService
    plural: virtualservices
    singular: virtualservice
    shortNames: [vs]
  versions:
    - name: v1alpha3
      served: true
      storage: false
      deprecated: true
      deprecationWarning: "networking.istio.io/v1alpha3 VirtualService is deprecated; use networking.istio.io/v1beta1"
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
    - name: v1beta1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/vs-crd.yaml"
wait_established virtualservices.networking.istio.io

kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
metadata:
  name: legacy-routing
  namespace: default
spec: {}
EOF

kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: reviews-route
  namespace: istio-system
spec: {}
---
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: frontend-route
  namespace: default
spec: {}
EOF

# ---------------------------------------------------------------------------
# Istio: DestinationRule — CRD ohne Instanzen (Kandidat für --unused)
# ---------------------------------------------------------------------------
echo "==> CRD: destinationrules.networking.istio.io (bewusst ohne Instanzen)"
cat > "$WORKDIR/dr-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: destinationrules.networking.istio.io
spec:
  group: networking.istio.io
  scope: Namespaced
  names:
    kind: DestinationRule
    plural: destinationrules
    singular: destinationrule
    shortNames: [dr]
  versions:
    - name: v1alpha3
      served: true
      storage: false
      deprecated: true
      deprecationWarning: "networking.istio.io/v1alpha3 DestinationRule is deprecated; use networking.istio.io/v1beta1"
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
    - name: v1beta1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/dr-crd.yaml"
wait_established destinationrules.networking.istio.io

# ---------------------------------------------------------------------------
# ArgoCD: Application + AppProject — je eine Version, keine Deprecation
# ---------------------------------------------------------------------------
echo "==> CRD: applications.argoproj.io"
cat > "$WORKDIR/app-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: applications.argoproj.io
spec:
  group: argoproj.io
  scope: Namespaced
  names:
    kind: Application
    plural: applications
    singular: application
    shortNames: [app, apps]
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/app-crd.yaml"
wait_established applications.argoproj.io

kubectl apply -f - <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: guestbook
  namespace: argocd
spec: {}
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: payments-service
  namespace: argocd
spec: {}
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: frontend
  namespace: argocd
spec: {}
EOF

echo "==> CRD: appprojects.argoproj.io"
cat > "$WORKDIR/appproject-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: appprojects.argoproj.io
spec:
  group: argoproj.io
  scope: Namespaced
  names:
    kind: AppProject
    plural: appprojects
    singular: appproject
  versions:
    - name: v1alpha1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/appproject-crd.yaml"
wait_established appprojects.argoproj.io

kubectl apply -f - <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: default
  namespace: argocd
spec: {}
EOF

# ---------------------------------------------------------------------------
# cert-manager: Certificate — deprecated v1alpha2 als Storage-Version anlegen,
# Instanzen erzeugen, dann Storage-Version auf v1 umschalten. Danach bleibt
# v1alpha2 in status.storedVersions stehen -> "Storage version migration
# candidate" in main.py.
#
# Zusätzlich mit Webhook-Konversionsstrategie (ohne caBundle, ohne echten
# Webhook-Service dahinter) -> "Webhook conversion targets" in main.py. Da
# nach dem Storage-Umschalten unten v1alpha2 nicht mehr die Storage-Version
# ist, versucht main.py beim Auflisten dieser Version zu konvertieren, was
# mangels erreichbarem Webhook fehlschlägt (main.py fängt das ab, siehe
# _count_instances_by_namespace in kubectl.py).
# ---------------------------------------------------------------------------
echo "==> CRD: certificates.cert-manager.io (Storage-Migrations- + Webhook-Konversions-Szenario)"
cat > "$WORKDIR/cert-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: certificates.cert-manager.io
spec:
  group: cert-manager.io
  scope: Namespaced
  names:
    kind: Certificate
    plural: certificates
    singular: certificate
    shortNames: [cert, certs]
  conversion:
    strategy: Webhook
    webhook:
      conversionReviewVersions: ["v1"]
      clientConfig:
        service:
          name: cert-manager-webhook
          namespace: cert-manager
          path: /convert
          port: 443
  versions:
    - name: v1alpha2
      served: true
      storage: true
      deprecated: true
      deprecationWarning: "cert-manager.io/v1alpha2 Certificate is deprecated; use cert-manager.io/v1"
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
    - name: v1
      served: true
      storage: false
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/cert-crd.yaml"
wait_established certificates.cert-manager.io

kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1alpha2
kind: Certificate
metadata:
  name: web-tls
  namespace: cert-manager
spec: {}
---
apiVersion: cert-manager.io/v1alpha2
kind: Certificate
metadata:
  name: api-tls
  namespace: cert-manager
spec: {}
EOF

echo "==> Schalte Storage-Version von v1alpha2 auf v1 um (erzeugt Migrations-Kandidat)"
kubectl patch crd certificates.cert-manager.io --type=json -p='[
  {"op": "replace", "path": "/spec/versions/0/storage", "value": false},
  {"op": "replace", "path": "/spec/versions/1/storage", "value": true}
]'

# ---------------------------------------------------------------------------
# cert-manager: ClusterIssuer — cluster-scoped CRD, keine Deprecation
# ---------------------------------------------------------------------------
echo "==> CRD: clusterissuers.cert-manager.io (cluster-scoped)"
cat > "$WORKDIR/clusterissuer-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: clusterissuers.cert-manager.io
spec:
  group: cert-manager.io
  scope: Cluster
  names:
    kind: ClusterIssuer
    plural: clusterissuers
    singular: clusterissuer
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/clusterissuer-crd.yaml"
wait_established clusterissuers.cert-manager.io

kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec: {}
EOF

# ---------------------------------------------------------------------------
# Namenskonflikt: zwei CRDs in derselben Gruppe mit demselben Kind. Die zweite
# bleibt mit status.conditions[NamesAccepted]=False (und dadurch auch nie
# Established) hängen -> "Unhealthy CRDs (status conditions)" in main.py.
# ---------------------------------------------------------------------------
echo "==> CRD: backends.routing.example.io (Kind: Backend)"
cat > "$WORKDIR/backend-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: backends.routing.example.io
spec:
  group: routing.example.io
  scope: Namespaced
  names:
    kind: Backend
    plural: backends
    singular: backend
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/backend-crd.yaml"
wait_established backends.routing.example.io

echo "==> CRD: endpointsets.routing.example.io (Kind: Backend — bewusster Namenskonflikt mit backends.routing.example.io)"
cat > "$WORKDIR/endpointsets-crd.yaml" <<'EOF'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: endpointsets.routing.example.io
spec:
  group: routing.example.io
  scope: Namespaced
  names:
    kind: Backend
    plural: endpointsets
    singular: endpointset
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
EOF
kubectl apply -f "$WORKDIR/endpointsets-crd.yaml"
echo "    (wird absichtlich NIE Established — NamesAccepted=False wegen Kind-Konflikt)"
kubectl wait --for=condition=Established "crd/endpointsets.routing.example.io" --timeout=15s \
    || echo "    -> wie erwartet nicht Established."

echo
echo "==> Fertig. Angelegte CRDs:"
kubectl get crd \
    virtualservices.networking.istio.io \
    destinationrules.networking.istio.io \
    applications.argoproj.io \
    appprojects.argoproj.io \
    certificates.cert-manager.io \
    clusterissuers.cert-manager.io \
    backends.routing.example.io \
    endpointsets.routing.example.io

echo
echo "Jetzt testen mit:"
echo "  python main.py"
echo "  python main.py --unused          # sollte destinationrules.networking.istio.io zeigen"
echo
echo "In der Ausgabe von 'python main.py' zusätzlich zu beachten:"
echo "  - Abschnitt 'Webhook conversion targets': certificates.cert-manager.io"
echo "    mit Ziel cert-manager-webhook.cert-manager:443/convert und Hinweis"
echo "    auf fehlendes caBundle"
echo "  - Abschnitt 'Unhealthy CRDs (status conditions)':"
echo "    endpointsets.routing.example.io mit NamesAccepted=False"
echo
echo "Aufräumen mit:"
echo "  ./test.sh cleanup"
