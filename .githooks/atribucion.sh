#!/bin/sh
# Rechaza la atribución a una herramienta de IA en el texto que llega por la
# entrada estándar. La usan el hook commit-msg y el job `atribucion` del CI,
# para que los dos apliquen exactamente la misma regla.
#
# Salida 0 si el texto está limpio; 1 si encuentra atribución (y la imprime).

patterns='co-authored-by:
generated (with|by)[[:space:]].*(claude|anthropic|chatgpt|openai|copilot|gemini|codex|cursor)
🤖[[:space:]]*generated
generated with \[?claude
claude\.(com|ai)/(claude-)?code
noreply@anthropic\.com'

found=$(grep -Ein "$patterns" 2>/dev/null)
if [ -n "$found" ]; then
  printf '%s\n' "$found" >&2
  exit 1
fi
exit 0
