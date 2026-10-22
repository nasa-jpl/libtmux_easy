# libtmux_easy: the rc file of a shell or wrap pane's bash, started as
#     bash --rcfile lte_rc.bash -i -s -- RCFILE
# Runs RCFILE (e.g. ~/.bashrc; '' for none) for its aliases, prompt and
# functions, then puts the exported environment back exactly as tmux started
# the shell with (/proc/$$/environ): an rc file can't change what the pane's
# commands get. Afterwards the shell is yours: export away. docs/environment.md

__lte_rc=${1-}
__lte_rc=${__lte_rc/#\~/$HOME}
set --
if [ -n "$__lte_rc" ] && [ -r "$__lte_rc" ]; then
    . "$__lte_rc"
fi
if [ -r "/proc/$$/environ" ]; then
    for __lte_n in $(compgen -e); do
        case $__lte_n in PWD|OLDPWD|SHLVL|_) ;; *) unset -v "$__lte_n" 2>/dev/null ;; esac
    done
    while IFS= read -r -d '' __lte_n; do
        case ${__lte_n%%=*} in PWD|OLDPWD|SHLVL|_) continue ;; esac
        export "$__lte_n" 2>/dev/null
    done < "/proc/$$/environ"
fi
unset -v __lte_rc __lte_n
