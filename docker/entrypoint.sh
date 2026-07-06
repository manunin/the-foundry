#!/bin/sh
set -eu

case "${FORGE_PROVIDER:-github}" in
  gitlab)
    raw_host="${GITLAB_HOST:-${GL_HOST:-https://gitlab.com}}"
    case "$raw_host" in
      http://*) api_protocol=http ;;
      *) api_protocol=https ;;
    esac
    host="${raw_host#https://}"
    host="${host#http://}"
    host="${host%/}"
    git_protocol="${GITLAB_GIT_PROTOCOL:-https}"
    case "$git_protocol" in
      https|ssh) ;;
      *) echo "GITLAB_GIT_PROTOCOL must be https or ssh" >&2; exit 2 ;;
    esac
    glab config set api_protocol "$api_protocol" --host "$host" >/dev/null
    glab config set git_protocol "$git_protocol" --host "$host" >/dev/null
    glab config set user oauth2 --host "$host" >/dev/null
    git config --global "credential.${api_protocol}://${host}.helper" '!glab auth git-credential'
    if [ "$git_protocol" = "https" ]; then
      git config --global "credential.${api_protocol}://${host}.username" oauth2
    fi
    ;;
  *)
    host="${GH_HOST:-github.com}"
    git config --global "credential.https://${host}.helper" '!gh auth git-credential'
    ;;
esac

git config --global --add safe.directory '*'

exec "$@"
