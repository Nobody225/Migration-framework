#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# install_converter_deps.sh
# Installation des dépendances du module Converter sur Rocky Linux
# ═══════════════════════════════════════════════════════════════

set -e

echo "=============================================="
echo "  Installation des dépendances Converter"
echo "  Orange Migration Framework"
echo "=============================================="
echo ""

# Vérifier qu'on est root
if [[ $EUID -ne 0 ]]; then
    echo "❌ Ce script doit être lancé en root (sudo bash install_converter_deps.sh)"
    exit 1
fi

OS=$(cat /etc/os-release | grep "^ID=" | cut -d= -f2 | tr -d '"')
echo "OS détecté: $OS"
echo ""

# ── 1. qemu-img (obligatoire) ────────────────────────────────
echo "→ Installation de qemu-img..."
if command -v qemu-img &>/dev/null; then
    echo "  ✓ qemu-img déjà installé: $(qemu-img --version | head -1)"
else
    dnf install -y qemu-img 2>/dev/null || \
    dnf install -y qemu-tools 2>/dev/null || \
    apt-get install -y qemu-utils 2>/dev/null || \
    echo "  ⚠ Installer manuellement qemu-img"
fi

# ── 2. libguestfs (pour la détection OS et l'injection VirtIO) ──
echo ""
echo "→ Installation de libguestfs..."
if command -v guestfish &>/dev/null; then
    echo "  ✓ libguestfs déjà installé"
else
    dnf install -y libguestfs-tools python3-libguestfs 2>/dev/null || \
    apt-get install -y libguestfs-tools python3-libguestfs 2>/dev/null || \
    echo "  ⚠ libguestfs non disponible sur ce système"
fi

# ── 3. python3-hivex (pour l'édition du registre Windows) ──────
echo ""
echo "→ Installation de python3-hivex..."
if python3 -c "import hivex" 2>/dev/null; then
    echo "  ✓ python3-hivex déjà installé"
else
    dnf install -y python3-hivex 2>/dev/null || \
    apt-get install -y python3-hivex 2>/dev/null || \
    echo "  ⚠ python3-hivex non disponible — injection registre désactivée"
fi

# ── 4. virtio-win (pilotes VirtIO Windows) ─────────────────────
echo ""
echo "→ Installation de virtio-win..."
if ls /usr/share/virtio-win/*.iso 2>/dev/null || ls /usr/share/virtio-win.iso 2>/dev/null; then
    echo "  ✓ virtio-win déjà installé"
else
    # Activer le dépôt Fedora pour virtio-win
    if command -v dnf &>/dev/null; then
        dnf install -y epel-release 2>/dev/null || true
        dnf install -y virtio-win 2>/dev/null || {
            echo "  ⚠ virtio-win non disponible via dnf"
            echo "  → Téléchargement direct..."
            VIRTIO_URL="https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win.iso"
            curl -Lo /tmp/virtio-win.iso "$VIRTIO_URL" 2>/dev/null && \
            echo "  ✓ virtio-win.iso téléchargé dans /tmp/" || \
            echo "  ⚠ Télécharger manuellement: $VIRTIO_URL"
        }
    fi
fi

# ── 5. Vérification finale ──────────────────────────────────────
echo ""
echo "=============================================="
echo "  Vérification finale"
echo "=============================================="

check_tool() {
    if command -v "$1" &>/dev/null; then
        echo "  ✅ $1"
    else
        echo "  ❌ $1 — MANQUANT"
    fi
}

check_python() {
    if python3 -c "import $1" 2>/dev/null; then
        echo "  ✅ python3-$1"
    else
        echo "  ⚠  python3-$1 — optionnel, non installé"
    fi
}

check_file() {
    if ls "$1" 2>/dev/null | head -1 | grep -q .; then
        echo "  ✅ $2: $(ls $1 2>/dev/null | head -1)"
    else
        echo "  ⚠  $2 — non trouvé (optionnel pour Windows)"
    fi
}

check_tool qemu-img
check_tool guestfish
check_python guestfs
check_python hivex
check_file "/usr/share/virtio-win/*.iso /usr/share/virtio-win.iso /tmp/virtio-win.iso" "virtio-win ISO"

echo ""
echo "Installation terminée."
echo ""
echo "Pour tester: python3 -c \"from src.converter import VMConverter; print('OK')\""
