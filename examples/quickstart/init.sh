#!/bin/sh
# Create the quickstart's managed repository: a git repository on branch develop holding
# examples/quickstart/repo. Writes only <target-dir>; prints the commands to run next.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <target-dir>" >&2
    exit 2
fi
here=$(cd "$(dirname "$0")" && pwd)
target=$1
if [ -e "$target" ] && [ -n "$(ls -A "$target")" ]; then
    echo "error: $target exists and is not empty" >&2
    exit 1
fi
mkdir -p "$target"
target=$(cd "$target" && pwd)
cp -R "$here/repo/." "$target/"
cd "$target"
git init -q -b develop
git add -A
git -c user.name="Master System quickstart" -c user.email=quickstart@localhost \
    -c commit.gpgsign=false commit -q -m "Quickstart seed"

projects=${XDG_CONFIG_HOME:-$HOME/.config}/master-system/projects
cat <<NEXT
Created $target (branch develop).

Next:
  mkdir -p $projects/quickstart
  cp $here/project/*.yaml $projects/quickstart/
  sed -i "s#^repository:.*#repository: $target#" $projects/quickstart/project.yaml
  ms backlog quickstart add "Add a farewell function"
  ms chat quickstart        # "plan epic 1 from the backlog", then: check, approve, quit
  ms daemon --once          # one service cycle in the foreground
  ms report                 # attempts, verdicts, tokens and cost
NEXT
