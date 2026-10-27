# Sourced by demo 2's shell scripts: the repository's Python, or python3.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
PYTHON=$ROOT/.venv/bin/python
[[ -x $PYTHON ]] || PYTHON=$(command -v python3)
export AWS_PROFILE=${AWS_PROFILE:-release-demo} AWS_REGION=${AWS_REGION:-eu-west-1}
