{{- define "appflowy-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "appflowy-mcp.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "appflowy-mcp.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "appflowy-mcp.labels" -}}
app.kubernetes.io/name: {{ include "appflowy-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "appflowy-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "appflowy-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "appflowy-mcp.secretName" -}}
{{- printf "%s-secrets" (include "appflowy-mcp.fullname" .) -}}
{{- end -}}
