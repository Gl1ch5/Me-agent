#!/bin/sh
# me installer — curl -fsSL __SERVER__/i | sh
set -e
SERVER="__SERVER__"
ME_HOME="${ME_HOME:-$HOME/.me}"
BIN="$HOME/.local/bin"
[ -n "$TERMUX_VERSION" ] && [ -n "$PREFIX" ] && BIN="$PREFIX/bin"   # Android / Termux
c() { printf '\033[38;2;139;124;255m%s\033[0m\n' "$1"; }

PY=""
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  if [ -n "$TERMUX_VERSION" ]; then pkg install -y python >/dev/null 2>&1 && PY=python; fi
  [ -z "$PY" ] && { echo "me needs Python 3.8+ — install it first."; exit 1; }
fi

c "◆ installing me"
mkdir -p "$ME_HOME" "$BIN"
curl -fsSL "$SERVER/me.py" -o "$ME_HOME/me.py.tmp" && mv "$ME_HOME/me.py.tmp" "$ME_HOME/me.py"
cat > "$BIN/me" <<EOF
#!/bin/sh
exec $(command -v "$PY") "$ME_HOME/me.py" "\$@"
EOF
chmod +x "$BIN/me"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    LINE="export PATH=\"\$HOME/.local/bin:\$PATH\""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      if [ -f "$rc" ] || [ "$rc" = "$HOME/.profile" ]; then
        grep -qs '.local/bin' "$rc" || printf '\n# me\n%s\n' "$LINE" >> "$rc"
      fi
    done
    [ -d "$HOME/.config/fish" ] && { mkdir -p "$HOME/.config/fish/conf.d"; echo "fish_add_path $BIN" > "$HOME/.config/fish/conf.d/me.fish"; }
    export PATH="$BIN:$PATH"
    ;;
esac

c "✓ installed — type 'me' to start (open a new terminal if the command is not found)"
if [ -t 1 ] && [ -e /dev/tty ]; then exec "$BIN/me" < /dev/tty; fi
