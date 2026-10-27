# The check after an upgrade: /health must report $Expected within
# $TimeoutSeconds.
Write-Result (Wait-Health $Expected $TimeoutSeconds)
