# kubernetes-crds

Dieses Tool dient dazu, die CustomResourceDefinitions (CRDs) eines Kubernetes-Clusters zu erfassen und auszuwerten. Es listet alle CRDs samt ihrer API-Versionen auf.
In einer Tabelle wird gezeigt, welche Version jeweils "served" bzw. "storage" ist, ob die CRD eine `Webhook`-Konversionsstrategie verwendet (`spec.conversion.strategy`), und ermittelt die Anzahl der tatsächlich existierenden Instanzen je Namespace (bzw. clusterweit bei cluster-scoped CRDs). 
Dadurch lässt sich nicht nur nachvollziehen, welche CRDs im Cluster installiert sind und wie sie genutzt werden, sondern auch, welche CRDs keine oder kaum Instanzen besitzen – also ungenutzte oder veraltete CRDs, die Kandidaten für eine Bereinigung sind.

## Installation

Voraussetzung: Python >= 3.14 sowie ein gültiger kubeconfig-Kontext (`~/.kube/config` bzw. In-Cluster-Config).

Mit [uv](https://docs.astral.sh/uv/):

```
uv sync
uv run python main.py
```

Mit `pip` (z.B. in einem virtuellen Environment):

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Verwendung

```
python main.py [-n NAMESPACE] [--unused] [--openshift] [--insecure-skip-tls-verify] [-v]
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
- `-v, --verbose`: Aktiviert Debug-Logging, z.B. für einzelne API-Aufrufe, die
  wegen eines Fehlers (z.B. fehlende RBAC-Berechtigung) übersprungen wurden.

Bei fehlender oder ungültiger kubeconfig bricht das Tool mit einer klaren
Fehlermeldung und Exitcode 1 ab, statt einen rohen Stacktrace auszugeben.

Die Spalte **CONVERSION** zeigt `Webhook`, wenn die CRD auf einen Konversions-
Webhook angewiesen ist, um zwischen ihren Versionen zu übersetzen (statt `None`
für keine Konversion). Ist der Webhook-Service nicht erreichbar oder
fehlkonfiguriert, schlagen API-Zugriffe auf nicht-Storage-Versionen fehl, auch
wenn diese laut `served: true` eigentlich verfügbar sein sollten – ein Punkt,
den diese Tabelle allein nicht prüfen kann, aber zumindest sichtbar macht.

Zusätzlich zur Tabelle werden zwei Hinweis-Abschnitte ausgegeben, sofern zutreffend:

- **Deprecated API versions**: Alle CRD-Versionen, die per `spec.versions[].deprecated`
  als veraltet markiert sind, samt der optionalen `deprecationWarning`-Meldung.
- **Storage version migration candidates**: CRDs, bei denen `status.storedVersions`
  noch ältere, nicht mehr aktuelle Storage-Versionen enthält – ein Hinweis, dass
  Instanzen noch nicht auf die aktuelle Storage-Version migriert wurden und die
  alte Version deshalb noch nicht aus der CRD entfernt werden darf.
- **Unhealthy CRDs (status conditions)**: CRDs, deren `status.conditions` vom Typ
  `Established` bzw. `NamesAccepted` nicht `True` sind (z.B. wegen eines Namens-
  konflikts). Eine solche CRD taucht in der Tabelle normal auf, aber jeder
  API-Zugriff darauf schlägt fehl – ohne diesen Hinweis wäre das nicht sichtbar.
- **Webhook conversion targets**: Für jede CRD mit `CONVERSION=Webhook` wird das
  Ziel aus `spec.conversion.webhook.clientConfig` angezeigt – entweder als
  `service.name.service.namespace:port/path` (bei einem clusterinternen Service)
  oder als externe URL – sowie ein Hinweis, falls kein `caBundle` konfiguriert
  ist. Das dient nur der manuellen Erreichbarkeitsprüfung; ob der Webhook
  tatsächlich erreichbar ist, wird nicht geprüft (siehe Hinweis zur Spalte
  CONVERSION oben).
- **Fetch errors**: Konnte die Instanzanzahl für eine CRD-Version in einem
  Namespace (bzw. clusterweit) nicht ermittelt werden (z.B. wegen eines nicht
  erreichbaren Konversions-Webhooks oder fehlender RBAC-Berechtigung), taucht
  in der Tabelle statt einer Zahl ein `?` auf. Dieser Abschnitt listet zu jedem
  `?` den zugrunde liegenden Fehlergrund auf, damit klar ist, dass es sich um
  einen unbekannten und nicht um einen tatsächlichen Nullwert handelt.

Alle API-Aufrufe sind mit einem Timeout von 30 Sekunden abgesichert, damit ein
einzelner hängender Request (z.B. wegen eines kaputten Konversions-Webhooks)
das Tool nicht unbegrenzt blockiert.

