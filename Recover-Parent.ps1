Set-Location -LiteralPath $PSScriptRoot
docker compose exec wealth-trainer python -m app.manage recover-parent
