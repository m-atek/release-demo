"""Run the Windows track on the Windows build agent without Jenkins, through Run Command.

    python scripts/demos/desktop/windows_try.py                               test and build this working copy there
    python scripts/demos/desktop/windows_try.py wix_sdk=WixToolset.Sdk/5.0.2  with a setting overridden

For trying changes before they reach main, and before the agent is connected
to Jenkins. Ships services/store-assistant, scripts/demos and services.yaml
to the demo's bucket, runs windows_build.ps1 test and build on the agent,
and brings dist/store-assistant/parts/windows/ back here.
"""

import io
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop  # noqa: E402
from desktop import ROOT, aws_session, fail  # noqa: E402

SHIPPED = ["services/store-assistant", "scripts/demos", "services.yaml"]
SKIP = {".build", "bin", "obj", "__pycache__", ".pytest_cache"}

RUN = r"""
$ErrorActionPreference = 'Stop'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$work = 'C:\try\{stamp}'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Read-S3Object -BucketName '{bucket}' -Key 'source/{stamp}.zip' -File "$work.zip" -Region eu-west-1 | Out-Null
Expand-Archive "$work.zip" $work -Force
Set-Location $work
$env:BUILD_NUMBER = 'try-{stamp}'
{env}
foreach ($step in 'test', 'build') {{
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demos\desktop\windows_build.ps1 -Step $step -Service store-assistant
  if ($LASTEXITCODE -ne 0) {{ throw "windows_build.ps1 $step failed: $LASTEXITCODE" }}
}}
Compress-Archive -Path dist\store-assistant\parts\windows\* -DestinationPath "$work-parts.zip" -Force
Write-S3Object -BucketName '{bucket}' -Key 'tryout/{stamp}/parts-windows.zip' -File "$work-parts.zip" -Region eu-west-1
"""


def main():
    settings = dict(arg.split("=", 1) for arg in sys.argv[1:])
    env = "\n".join(f"$env:{name} = '{value}'" for name, value in settings.items())
    session = aws_session()
    bucket = desktop.installer_bucket(session)
    instance = desktop.windows_instance(session, "release-demo-desktop-build")
    if instance is None:
        fail("The Windows build agent isn't running: scripts/demo.sh up desktop")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        for top in SHIPPED:
            for path in [ROOT / top] if (ROOT / top).is_file() else sorted((ROOT / top).rglob("*")):
                if path.is_file() and not SKIP & set(path.relative_to(ROOT).parts):
                    out.write(path, path.relative_to(ROOT).as_posix())
    session.client("s3").put_object(Bucket=bucket, Key=f"source/{stamp}.zip", Body=archive.getvalue())
    print(f"Shipped {len(archive.getvalue()) // 1024} KB; running on {instance}")
    status, output = desktop.run_powershell(session, instance, [RUN.format(stamp=stamp, bucket=bucket, env=env)],
                                            "Try the store-assistant Windows build", timeout=3000)
    print(output[-12000:])
    if status != "Success":
        fail(f"Run Command {status}")
    parts = session.client("s3").get_object(Bucket=bucket, Key=f"tryout/{stamp}/parts-windows.zip")["Body"].read()
    target = ROOT / "dist" / "store-assistant" / "parts" / "windows"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(parts)) as archive:
        for entry in archive.infolist():  # Windows PowerShell writes backslashes in zip paths
            path = target / entry.filename.replace("\\", "/")
            if not entry.filename.endswith(("/", "\\")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(entry))
    print(f"Parts in {target.relative_to(ROOT)}: {', '.join(sorted(p.name for p in target.iterdir()))}")


if __name__ == "__main__":
    main()
