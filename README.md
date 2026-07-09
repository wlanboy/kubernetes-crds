# kubernetes-crds

Dieses Tool dient dazu, die CustomResourceDefinitions (CRDs) eines Kubernetes-Clusters zu erfassen und auszuwerten. Es listet alle CRDs samt ihrer API-Versionen auf.
In einer Tabelle wird gezeigt, welche Version jeweils "served" bzw. "storage" ist, und ermittelt die Anzahl der tatsächlich existierenden Instanzen je Namespace (bzw. clusterweit bei cluster-scoped CRDs). 
Dadurch lässt sich nicht nur nachvollziehen, welche CRDs im Cluster installiert sind und wie sie genutzt werden, sondern auch, welche CRDs keine oder kaum Instanzen besitzen – also ungenutzte oder veraltete CRDs, die Kandidaten für eine Bereinigung sind.

## Verwendung

```
python main.py [-n NAMESPACE] [--unused] [--openshift] [--insecure-skip-tls-verify]
```

- `-n, --namespace NAMESPACE`: Beschränkt die Auswertung auf einen Namespace
  (ohne Angabe werden alle Namespaces sowie cluster-scoped CRDs erfasst).
- `--unused`: Listet nur die CRDs auf, die in keiner Version/keinem Namespace
  Instanzen besitzen – praktisch, um Kandidaten für eine Bereinigung zu finden.
- `--openshift`: Bezieht zusätzlich OpenShift-spezifische API-Ressourcen mit ein
  (z.B. `Route`, `BuildConfig`, `DeploymentConfig`), die als eingebaute
  aggregierte APIs in `*.openshift.io`-Gruppen laufen und daher keine
  CustomResourceDefinitions sind (siehe [oc.py](oc.py)). Ohne diesen Schalter
  tauchen sie nicht in der Auswertung auf.
- `--insecure-skip-tls-verify`: Deaktiviert die TLS-Zertifikatsprüfung gegenüber
  dem API-Server (analog zu `kubectl`/`oc --insecure-skip-tls-verify`) –
  nützlich bei Clustern mit selbstsignierten Zertifikaten.

