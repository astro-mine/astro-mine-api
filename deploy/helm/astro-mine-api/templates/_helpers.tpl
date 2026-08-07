{{/* Common name + label helpers for the Astro-Mine REST tier chart. */}}

{{- define "astro-mine-api.name" -}}
astro-mine-api
{{- end -}}

{{- define "astro-mine-api.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "astro-mine-api.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* The standard label set, matching the platform chart's shape. */}}
{{- define "astro-mine-api.labels" -}}
app.kubernetes.io/name: {{ include "astro-mine-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: astro-mine
helm.sh/chart: astro-mine-api-{{ .Chart.Version }}
{{- end -}}

{{- define "astro-mine-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "astro-mine-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The image reference. A digest wins over a tag when both are set: pinning by digest is what makes
"which image was running" answerable after the fact (CX-REPRO), and a release that sets both almost
always means the digest.
*/}}
{{- define "astro-mine-api.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}
{{- end -}}
