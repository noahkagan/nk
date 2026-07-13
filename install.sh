#!/bin/sh
set -eu

source_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
nk_home=${NK_HOME:-"$HOME/.nk"}
stage="$nk_home/.install.$$"
revision=$(git -C "$source_root" rev-parse HEAD)
if [ -n "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]; then
  revision="$revision-dirty"
fi

cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$nk_home" "$nk_home/clusters" "$stage/app" "$stage/bin" "$stage/skills"
cp -R "$source_root/nk" "$stage/app/nk"
cp -R "$source_root/entrypoints" "$stage/app/entrypoints"
cp -R "$source_root/prompts" "$stage/app/prompts"
printf '%s\n' "$revision" > "$stage/app/REVISION"
cp "$source_root/bin/nk" "$stage/bin/nk"
cp "$source_root/bin/nk.cmd" "$stage/bin/nk.cmd"
cp -R "$source_root/skills/." "$stage/skills/"
chmod +x "$stage/bin/nk"

rm -rf "$nk_home/app" "$nk_home/bin" "$nk_home/skills"
mv "$stage/app" "$nk_home/app"
mv "$stage/bin" "$nk_home/bin"
mv "$stage/skills" "$nk_home/skills"

mkdir -p "$HOME/.local/bin" "$HOME/.agents/skills" "$HOME/.claude/skills"
ln -sfn "$nk_home/bin/nk" "$HOME/.local/bin/nk"
for skill in "$nk_home"/skills/*; do
  name=$(basename "$skill")
  for discovery in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
    destination="$discovery/$name"
    rm -rf "$destination"
    ln -sfn "$skill" "$destination"
  done
done

echo "installed nk at $nk_home"
