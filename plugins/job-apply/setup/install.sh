#!/usr/bin/env bash
# One-step setup for job-apply, the Claude Code plugin that applies to jobs for you (macOS, Linux).
#
# Paste this into Terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.sh | bash
#
# With your resume:            ... | bash -s -- --resume ~/Documents/resume.pdf
# To see the plan, change nothing: ... | bash -s -- --dry-run
#
# Each step looks first and only acts when something is missing, so it's safe to run again (to
# update the plugin, say). It never asks for a password: site passwords go in the Job Desk page.
# Windows has its own: install.ps1, next to this file.

MARKETPLACE_REPO="sebob2491/Jobs"
MARKETPLACE="sebob-jobs"
PLUGIN="job-apply@sebob-jobs"
CLAUDE_INSTALL="curl -fsSL https://claude.ai/install.sh | bash"
UV_INSTALL="curl -LsSf https://astral.sh/uv/install.sh | sh"
RESUME_TYPES="pdf docx doc rtf odt txt"

DRY_RUN=0
YES=0
RESUME=""
FAILED=0
CLAUDE=""
UV=""
SERVER=""
PLUGIN_INSTALLED=0
PLUGIN_ENABLED=1
PLUGIN_PATH=""

usage() {
    cat <<'EOF'
One-step setup for the job-apply plugin for Claude Code.

  --resume PATH   copy your resume into ~/.job-apply for setup to read
  --dry-run       say what it would do, and change nothing
  --yes           don't ask; take the default answer (yes) everywhere
  --help          this help
EOF
}

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
done_() { printf '  OK: %s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
problem() { printf '  ! %s\n' "$*"; FAILED=1; }

can_ask() { ( : </dev/tty ) 2>/dev/null; }

# ask "Question?": yes unless the person says no (with no keyboard to ask on, or --yes: yes)
ask() {
    if [ "$YES" = 1 ] || ! can_ask; then
        note "$1 Yes."
        return 0
    fi
    printf '  %s [Y/n] ' "$1"
    local reply=""
    read -r reply </dev/tty || reply=""
    case "$reply" in [Nn]*) return 1 ;; esac
    return 0
}

# run "what it's for" command args...: said first, or only said in a dry run
run() {
    local what="$1"
    shift
    if [ "$DRY_RUN" = 1 ]; then
        note "Would run: $(shown "$@")   ($what)"
        return 0
    fi
    note "$what: $(shown "$@")"
    "$@" </dev/null
    local status=$?
    [ $status -eq 0 ] || problem "That didn't work (exit code $status)."
    return $status
}

# a command as a person would type it: the program's own name, not its whole path
shown() {
    local first="${1##*/}"
    shift
    printf '%s' "$first"
    local a
    for a in "$@"; do
        case "$a" in *" "*) printf ' "%s"' "$a" ;; *) printf ' %s' "$a" ;; esac
    done
}

add_path() {
    case ":$PATH:" in *":$1:"*) return 0 ;; esac
    [ -d "$1" ] && PATH="$1:$PATH" && export PATH
    return 0
}

# Claude Code's and uv's installers put them here; a terminal opened before they ran hasn't it
refresh_path() {
    add_path "$HOME/.local/bin"
    add_path "$HOME/.cargo/bin"
    hash -r 2>/dev/null
}

# ---------------------------------------------------------------- 1. git

check_git() {
    step "1 of 8: Git (Claude Code uses it to fetch the plugin)"
    local git_path
    git_path=$(command -v git 2>/dev/null)
    if [ "$(uname -s)" = Darwin ] && [ "$git_path" = /usr/bin/git ] && ! xcode-select -p >/dev/null 2>&1; then
        git_path=""  # macOS's stand-in: the real one comes with Apple's command line tools
    fi
    if [ -n "$git_path" ]; then
        done_ "Git is installed."
        return 0
    fi
    if [ "$(uname -s)" = Darwin ]; then
        note "Git comes with Apple's command line developer tools, which aren't installed yet."
        if [ "$DRY_RUN" = 1 ]; then
            note "Would ask to install them (xcode-select --install), then ask you to run this again."
        elif ask "Install them now? A window opens: click Install."; then
            xcode-select --install >/dev/null 2>&1
            note "When that window says it's done, run this installer again."
        fi
    else
        note "Install Git with your system's package manager (for example: sudo apt install git),"
        note "then run this installer again."
    fi
    [ "$DRY_RUN" = 1 ] || problem "Git isn't installed yet, so the plugin can't be fetched."
    return 1
}

# ---------------------------------------------------------------- 2. Claude Code

check_claude() {
    step "2 of 8: Claude Code"
    CLAUDE=$(command -v claude 2>/dev/null)
    if [ -n "$CLAUDE" ]; then
        done_ "Claude Code is installed (version $("$CLAUDE" --version </dev/null 2>/dev/null | head -n 1 | cut -d' ' -f1))."
        return 0
    fi
    note "Claude Code isn't installed. Its official installer is: $CLAUDE_INSTALL"
    if [ "$DRY_RUN" = 1 ]; then
        note "Would ask to run it (the answer is yes unless you say no)."
        return 1
    fi
    if ask "Install Claude Code now?"; then
        note "Installing Claude Code..."
        curl -fsSL https://claude.ai/install.sh | bash
        refresh_path
        CLAUDE=$(command -v claude 2>/dev/null)
    fi
    if [ -z "$CLAUDE" ]; then
        problem "Claude Code isn't installed. Install it (https://claude.com/claude-code), then run this again."
        return 1
    fi
    done_ "Claude Code is installed."
}

# ---------------------------------------------------------------- 3. the plugin

# What Claude Code says about the plugin: PLUGIN_INSTALLED, PLUGIN_ENABLED, PLUGIN_PATH
plugin_info() {
    PLUGIN_INSTALLED=0
    PLUGIN_ENABLED=1
    PLUGIN_PATH=""
    [ -n "$CLAUDE" ] || return 0
    local info
    info=$("$CLAUDE" plugin list --json </dev/null 2>/dev/null | tr -d '\n' |
        grep -oE '"(id|installPath)": *"[^"]*"|"enabled": *(true|false)' |
        awk -F'"' -v want="$PLUGIN" '
            $2 == "id" { mine = ($4 == want); if (mine) found = 1 }
            mine && $2 == "enabled" { enabled = ($0 ~ /false/) ? 0 : 1 }
            mine && $2 == "installPath" && path == "" { path = $4 }
            END { if (found) printf "1 %s %s", (enabled == "" ? 1 : enabled), path }')
    if [ -n "$info" ]; then
        PLUGIN_INSTALLED=1
        PLUGIN_ENABLED=$(printf '%s' "$info" | cut -d' ' -f2)
        PLUGIN_PATH=$(printf '%s' "$info" | cut -d' ' -f3-)
    fi
}

install_plugin() {
    step "3 of 8: The job-apply plugin"
    if [ -z "$CLAUDE" ]; then
        if [ "$DRY_RUN" = 1 ]; then
            note "Would run, once Claude Code is installed: claude plugin marketplace add $MARKETPLACE_REPO"
            note "Would run: claude plugin install $PLUGIN"
        else
            problem "Skipped: it needs Claude Code (above)."
        fi
        return 1
    fi
    if "$CLAUDE" plugin marketplace list --json </dev/null 2>/dev/null | grep -q "\"name\": *\"$MARKETPLACE\""; then
        run "Getting the newest list of the marketplace's plugins" "$CLAUDE" plugin marketplace update "$MARKETPLACE"
    else
        run "Adding the plugin's marketplace ($MARKETPLACE) to Claude Code" "$CLAUDE" plugin marketplace add "$MARKETPLACE_REPO"
    fi
    plugin_info
    if [ "$PLUGIN_INSTALLED" = 1 ]; then
        run "Updating the plugin to its newest version" "$CLAUDE" plugin update "$PLUGIN"
        if [ "$PLUGIN_ENABLED" = 0 ]; then
            run "Turning the plugin back on" "$CLAUDE" plugin enable "$PLUGIN"
        fi
    else
        run "Installing the plugin" "$CLAUDE" plugin install "$PLUGIN" || return 1
    fi
    [ "$DRY_RUN" = 1 ] || done_ "The plugin is installed."
}

# ---------------------------------------------------------------- 4. uv

check_uv() {
    step "4 of 8: uv (it runs the plugin's Python server)"
    UV=$(command -v uv 2>/dev/null)
    if [ -n "$UV" ]; then
        done_ "uv is installed (version $("$UV" --version </dev/null 2>/dev/null | head -n 1 | cut -d' ' -f2))."
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        note "Would install it with Astral's official installer: $UV_INSTALL"
        return 1
    fi
    note "Installing uv with Astral's official installer: $UV_INSTALL"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    refresh_path
    UV=$(command -v uv 2>/dev/null)
    if [ -z "$UV" ]; then
        problem "uv didn't install. See https://docs.astral.sh/uv/getting-started/installation/ and run this again."
        return 1
    fi
    done_ "uv is installed."
}

# ---------------------------------------------------------------- 5. the plugin's server

newest_version_dir() {
    # the highest version number among the folders given
    local d
    for d in "$@"; do [ -d "$d/server" ] && printf '%s\n' "${d##*/} $d"; done |
        sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1 | cut -d' ' -f2-
}

# Where Claude Code put the plugin: what it says, its list of installed plugins, its cache
find_server() {
    SERVER=""
    local config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}" dir=""
    plugin_info
    [ -n "$PLUGIN_PATH" ] && dir="$PLUGIN_PATH"
    if [ -z "$dir" ] && [ -f "$config/plugins/installed_plugins.json" ]; then
        dir=$(tr -d '\n' <"$config/plugins/installed_plugins.json" |
            grep -oE '"[^"]+@[^"]+": *\[|"installPath": *"[^"]*"' |
            awk -F'"' -v want="$PLUGIN" '$2 != "installPath" { mine = ($2 == want) } mine && $2 == "installPath" { print $4; exit }')
    fi
    if [ -z "$dir" ] || [ ! -f "$dir/server/pyproject.toml" ]; then
        dir=$(newest_version_dir "$config/plugins/cache/$MARKETPLACE/job-apply"/*)
    fi
    if [ -n "$dir" ] && [ -f "$dir/server/pyproject.toml" ]; then
        SERVER="$dir/server"
    fi
}

prepare_server() {
    step "5 of 8: The plugin's Python packages (so its first start is quick)"
    find_server
    if [ -z "$SERVER" ]; then
        if [ "$DRY_RUN" = 1 ]; then
            note "Would find the plugin's folder once it's installed, and run: uv sync --frozen there."
        else
            problem "Couldn't find where Claude Code put the plugin. Run \`claude plugin list\` to check it's installed."
        fi
        return 1
    fi
    note "The plugin is in: $SERVER"
    if [ -z "$UV" ]; then
        if [ "$DRY_RUN" = 1 ]; then
            note "Would run, once uv is installed: uv sync --frozen --project \"$SERVER\""
        else
            problem "Skipped: it needs uv (above)."
        fi
        return 1
    fi
    run "Installing the plugin's Python packages" "$UV" sync --frozen --project "$SERVER"
}

# ---------------------------------------------------------------- 6. a browser

find_browser() {
    local browsers="${PLAYWRIGHT_BROWSERS_PATH:-}" b
    if [ "$(uname -s)" = Darwin ]; then
        for b in "/Applications/Google Chrome.app" "$HOME/Applications/Google Chrome.app"; do
            [ -d "$b" ] && { say "Google Chrome"; return 0; }
        done
        for b in "/Applications/Microsoft Edge.app" "$HOME/Applications/Microsoft Edge.app"; do
            [ -d "$b" ] && { say "Microsoft Edge"; return 0; }
        done
        [ -n "$browsers" ] || browsers="$HOME/Library/Caches/ms-playwright"
    else
        if [ -x /opt/google/chrome/chrome ] || command -v google-chrome >/dev/null 2>&1 ||
            command -v google-chrome-stable >/dev/null 2>&1; then
            say "Google Chrome"
            return 0
        fi
        if [ -x /opt/microsoft/msedge/msedge ] || command -v microsoft-edge >/dev/null 2>&1 ||
            command -v microsoft-edge-stable >/dev/null 2>&1; then
            say "Microsoft Edge"
            return 0
        fi
        [ -n "$browsers" ] || browsers="${XDG_CACHE_HOME:-$HOME/.cache}/ms-playwright"
    fi
    for b in "$browsers"/chromium-*; do
        [ -d "$b" ] && { say "the plugin's own Chromium"; return 0; }
    done
    return 1
}

check_browser() {
    step "6 of 8: A browser for the plugin to fill applications in"
    local found
    if found=$(find_browser); then
        done_ "Found $found."
        return 0
    fi
    note "No Google Chrome or Microsoft Edge here, so the plugin gets its own Chromium (about 150 MB)."
    if [ -z "$UV" ] || [ -z "$SERVER" ]; then
        if [ "$DRY_RUN" = 1 ]; then
            note "Would run, in the plugin's folder: uv run --frozen playwright install chromium"
        else
            problem "Skipped: it needs uv and the plugin (above). Or install Google Chrome."
        fi
        return 1
    fi
    run "Downloading Chromium" "$UV" run --frozen --project "$SERVER" playwright install chromium
}

# ---------------------------------------------------------------- 7. your folder and resume

existing_resume() {
    local t
    for t in $RESUME_TYPES; do
        [ -f "$JA_HOME/resume.$t" ] && { printf '%s\n' "$JA_HOME/resume.$t"; return 0; }
    done
    return 1
}

# A path as typed or dragged in: quotes, "\ " for a space, a leading ~
clean_path() {
    local p="$1"
    p="${p#"${p%%[![:space:]]*}"}"
    p="${p%"${p##*[![:space:]]}"}"
    case "$p" in \"*\") p="${p#\"}"; p="${p%\"}" ;; \'*\') p="${p#\'}"; p="${p%\'}" ;; esac
    [ -e "$p" ] || p=$(printf '%s' "$p" | sed 's/\\\(.\)/\1/g')
    case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#\~/}" ;; esac
    printf '%s' "$p"
}

copy_resume() {
    local src="$1" ext dest stamp other
    if [ ! -f "$src" ]; then
        problem "There's no file at $src, so no resume was copied. Setup can ask for it instead."
        return 1
    fi
    ext=$(printf '%s' "${src##*.}" | tr '[:upper:]' '[:lower:]')
    case " $RESUME_TYPES " in
        *" $ext "*) ;;
        *) problem "$src isn't a PDF or Word file, so it wasn't copied. Setup can ask for it instead."; return 1 ;;
    esac
    dest="$JA_HOME/resume.$ext"
    if [ -f "$dest" ] && cmp -s "$src" "$dest" 2>/dev/null; then
        done_ "Your resume is already in your job-apply folder (resume.$ext)."
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        note "Would copy your resume to $dest (keeping any resume already there under another name)."
        return 0
    fi
    stamp=$(date +%Y%m%d-%H%M%S)
    for other in $RESUME_TYPES; do  # an older resume stays, under another name, so setup reads the new one
        [ -f "$JA_HOME/resume.$other" ] && mv "$JA_HOME/resume.$other" "$JA_HOME/resume-before-$stamp.$other"
    done
    if cp "$src" "$dest"; then
        done_ "Copied your resume to $dest. Setup reads it from there."
    else
        problem "Couldn't copy your resume to $dest."
        return 1
    fi
}

prepare_home() {
    local existing
    step "7 of 8: Your job-apply folder (your profile and resume stay on this computer, in it)"
    JA_HOME="${JOB_APPLY_HOME:-$HOME/.job-apply}"
    case "$JA_HOME" in "~") JA_HOME="$HOME" ;; "~/"*) JA_HOME="$HOME/${JA_HOME#\~/}" ;; esac
    if [ -d "$JA_HOME" ]; then
        done_ "It's there: $JA_HOME"
    elif [ "$DRY_RUN" = 1 ]; then
        note "Would make it: $JA_HOME"
    elif mkdir -p "$JA_HOME" && chmod 700 "$JA_HOME"; then
        done_ "Made it: $JA_HOME"
    else
        problem "Couldn't make $JA_HOME."
        return 1
    fi
    if [ -z "$RESUME" ] && ! existing_resume >/dev/null && [ "$DRY_RUN" = 0 ] && [ "$YES" = 0 ] && can_ask; then
        note "Your resume (optional): drag the file into this window, or type its path, then press Enter."
        printf '  Press Enter alone to skip: '
        read -r RESUME </dev/tty || RESUME=""
    fi
    if [ -n "$RESUME" ]; then
        copy_resume "$(clean_path "$RESUME")"
    elif existing=$(existing_resume); then
        done_ "Your resume is there (${existing##*/})."
    else
        note "No resume yet: Claude asks for it during setup."
    fi
    return 0
}

# ---------------------------------------------------------------- 8. the doctor

run_doctor() {
    step "8 of 8: Checking everything (the plugin's doctor)"
    if [ -z "$UV" ] || [ -z "$SERVER" ]; then
        note "It runs once uv and the plugin are in place (run this installer again then)."
        return 0
    fi
    if ! grep -q job-apply-doctor "$SERVER/pyproject.toml" 2>/dev/null; then
        note "This version of the plugin has no doctor yet: it comes with the next update."
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        note "Would run: uv run --frozen --project \"$SERVER\" job-apply-doctor --launch"
        return 0
    fi
    say ""
    "$UV" run --quiet --frozen --project "$SERVER" job-apply-doctor --launch </dev/null
    local status=$?
    say ""
    if [ $status -ne 0 ]; then
        note "Your profile and resume are what setup does next. For anything else the doctor"
        note "marked, the line under it says what to do."
    fi
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run | -n) DRY_RUN=1 ;;
            --yes | -y) YES=1 ;;
            --resume) shift; RESUME="${1:-}" ;;
            --resume=*) RESUME="${1#--resume=}" ;;
            -h | --help) usage; return 0 ;;
            *) say "Unknown option: $1"; usage; return 2 ;;
        esac
        shift
    done

    say "Setting up job-apply, the Claude Code plugin that applies to jobs for you."
    [ "$DRY_RUN" = 1 ] && say "Dry run: this says what it would do, and changes nothing."
    refresh_path

    local have_git=1
    check_git || have_git=0
    check_claude
    if [ "$have_git" = 1 ] || [ "$DRY_RUN" = 1 ]; then
        install_plugin
    else
        step "3 of 8: The job-apply plugin"
        problem "Skipped: it needs Git (above)."
    fi
    check_uv
    prepare_server
    check_browser
    prepare_home
    run_doctor

    say ""
    if [ "$DRY_RUN" = 1 ]; then
        say "That's the plan. Run it without --dry-run to do it."
        [ "$FAILED" = 0 ] || say "(Fix what's marked ! above first.)"
        [ "$FAILED" = 0 ]
        return
    fi
    if [ "$FAILED" = 1 ]; then
        say "Some steps didn't finish (marked ! above). Fix those, then run this again: it picks up where it left off."
        return 1
    fi
    say "All set. Next: open the Claude app (or type claude in a terminal) and say \"set up job-apply\"."
    say "Claude reads your resume and asks the few questions it can't answer. If the Claude app was open, quit and reopen it first."
    return 0
}

main "$@"
