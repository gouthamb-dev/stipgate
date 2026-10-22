# End-to-end smoke test against a deployed StipGate: healthz, auth check, the four core samples through
# Decider 2B (gate) and Nova Lite (extraction), one plain-text upload, then metrics.
# Expected actions: PROCEED, HALTED, PROCEED, HALTED.
#
#   pwsh deploy/aws/smoke-test.ps1 -BaseUrl https://<id>.cloudfront.net
#
# The API key is read from SSM (/stipgate/api-key) and only ever held in memory.
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [string]$Region = "us-east-1"
)
$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")
$key = "$(aws ssm get-parameter --name /stipgate/api-key --with-decryption --query Parameter.Value --output text --region $Region)".Trim()
$h = @{ "X-API-Key" = $key }
Remove-Variable key

"== healthz"
Invoke-RestMethod "$BaseUrl/healthz" | ConvertTo-Json -Depth 5 -Compress
"== unauthenticated /v1 call (expect 401)"
try { Invoke-RestMethod "$BaseUrl/v1/samples" | Out-Null; "UNEXPECTED: no auth required" }
catch { "status $($_.Exception.Response.StatusCode.value__)" }

$samples = (Invoke-RestMethod "$BaseUrl/v1/samples" -Headers $h).samples
$fail = 0
foreach ($d in $samples) {
    $body = @{ document_id = $d.document_id; snippet = $d.snippet; condition_code = $d.condition_code } | ConvertTo-Json
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod "$BaseUrl/v1/documents/evaluate" -Method Post -Headers $h -ContentType "application/json" -Body $body
    $ok = if ($r.gated_action -eq $d.expected_action) { "ok" } else { $fail++; "MISMATCH" }
    "== {0} [{1}] expected={2} got={3} {4} conf={5} status={6} tokens={7} wall={8} ms" -f `
        $d.document_id, $d.category, $d.expected_action, $r.gated_action, $ok, $r.confidence, $r.clearance_status, $r.tokens_spent, $sw.ElapsedMilliseconds
}

"== upload (plain text, sample SG-001)"
$tmp = Join-Path ([IO.Path]::GetTempPath()) "stipgate-smoke.txt"
[IO.File]::WriteAllText($tmp, $samples[0].snippet)
$u = Invoke-RestMethod "$BaseUrl/v1/documents/upload?document_id=SMOKE-UPLOAD&condition_code=$($samples[0].condition_code)" `
    -Method Post -Headers $h -ContentType "text/plain" -InFile $tmp
Remove-Item $tmp
"upload: {0} ocr={1} chars={2} status={3}" -f $u.gated_action, $u.ocr.method, $u.ocr.chars, $u.clearance_status

"== metrics"
Invoke-RestMethod "$BaseUrl/v1/metrics" -Headers $h | ConvertTo-Json -Depth 5 -Compress
if ($fail) { throw "$fail sample(s) did not match the expected action" }
