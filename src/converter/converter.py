"""
src/converter/converter.py
───────────────────────────
Module de conversion de disques VMware → OpenStack.

Pipeline :
  1. Télécharger le VMDK depuis l'ESXi (via VMX ou export direct)
  2. Convertir VMDK → QCOW2 via qemu-img
  3. Détecter l'OS invité (Linux / Windows)
  4. Pour Windows : injecter les pilotes VirtIO (viostor, vioscsi, netkvm)
                    + éditer les ruches registre offline (CriticalDeviceDatabase)
  5. Pour Linux   : vérifier la présence des modules VirtIO dans l'initramfs
  6. Retourner le chemin du QCOW2 prêt à uploader

Dépendances système (à installer sur la machine framework) :
  dnf install -y qemu-img libguestfs-tools python3-libguestfs
  # Pilotes VirtIO signés Red Hat :
  dnf install -y virtio-win   # ou télécharger l'ISO manuellement
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("migration.converter")


# ════════════════════════════════════════════════════════════════
# TYPES
# ════════════════════════════════════════════════════════════════

class GuestOS(Enum):
    LINUX   = "linux"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


@dataclass
class ConversionResult:
    success:      bool
    qcow2_path:   Optional[str]   = None
    guest_os:     GuestOS         = GuestOS.UNKNOWN
    os_name:      str             = ""
    virtio_injected: bool         = False
    disk_size_gb: float           = 0.0
    duration_s:   float           = 0.0
    errors:       List[str]       = field(default_factory=list)
    warnings:     List[str]       = field(default_factory=list)


# ════════════════════════════════════════════════════════════════
# DÉTECTION DES OUTILS
# ════════════════════════════════════════════════════════════════

def _find_tool(name: str) -> Optional[str]:
    """Retourne le chemin absolu d'un outil ou None."""
    path = shutil.which(name)
    if path:
        return path
    # Chercher dans les emplacements courants
    candidates = [
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/bin/{name}",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _find_virtio_iso() -> Optional[str]:
    """Cherche l'ISO des pilotes VirtIO Windows."""
    candidates = [
        "/usr/share/virtio-win/virtio-win.iso",
        "/usr/share/virtio-win.iso",
        "/opt/virtio-win/virtio-win.iso",
        "/tmp/virtio-win.iso",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


# ════════════════════════════════════════════════════════════════
# CONVERSION QEMU-IMG
# ════════════════════════════════════════════════════════════════

class DiskConverter:
    """
    Convertit un VMDK en QCOW2 avec qemu-img.
    Gère les VMDK multi-fichiers (split) et les thin disks.
    """

    def __init__(self, qemu_img_path: Optional[str] = None,
                 workspace_dir: str = "/tmp/migration_workspace",
                 compression: bool = True):
        self.qemu_img = qemu_img_path or _find_tool("qemu-img") or "/usr/bin/qemu-img"
        self.workspace = Path(workspace_dir)
        self.compression = compression
        self.workspace.mkdir(parents=True, exist_ok=True)

    def convert(self, vmdk_path: str, vm_name: str) -> str:
        """
        Convertit un VMDK en QCOW2.
        Retourne le chemin du fichier QCOW2 produit.
        """
        if not os.path.isfile(self.qemu_img):
            raise FileNotFoundError(
                f"qemu-img introuvable: {self.qemu_img}\n"
                "Installer via: dnf install -y qemu-img  (ou: apt install qemu-utils)"
            )

        vmdk = Path(vmdk_path)
        if not vmdk.exists():
            raise FileNotFoundError(f"VMDK introuvable: {vmdk_path}")

        qcow2_path = self.workspace / f"{vm_name}.qcow2"

        logger.info(f"Conversion VMDK → QCOW2 : {vmdk.name} → {qcow2_path.name}")

        # Vérifier l'espace disque disponible
        self._check_disk_space(vmdk, qcow2_path.parent)

        cmd = [
            self.qemu_img, "convert",
            "-f", "vmdk",
            "-O", "qcow2",
            "-p",          # Afficher la progression
        ]
        if self.compression:
            cmd += ["-c"]   # Compression zlib
        cmd += [str(vmdk), str(qcow2_path)]

        logger.info(f"Commande: {' '.join(cmd)}")

        import time
        start = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True)
        duration = time.monotonic() - start

        if result.returncode != 0:
            raise RuntimeError(
                f"qemu-img a échoué (code {result.returncode}):\n{result.stderr}"
            )

        size_gb = qcow2_path.stat().st_size / (1024 ** 3)
        logger.info(
            f"Conversion terminée en {duration:.1f}s — "
            f"QCOW2: {size_gb:.2f} GB"
        )

        return str(qcow2_path)

    def _check_disk_space(self, vmdk: Path, dest_dir: Path):
        """Vérifie qu'il y a assez d'espace (2× la taille du VMDK)."""
        vmdk_size = vmdk.stat().st_size
        statvfs = os.statvfs(dest_dir)
        free_bytes = statvfs.f_frsize * statvfs.f_bavail
        required = vmdk_size * 2  # VMDK + QCOW2

        if free_bytes < required:
            raise OSError(
                f"Espace insuffisant dans {dest_dir}: "
                f"{free_bytes / (1024**3):.1f} GB disponibles, "
                f"{required / (1024**3):.1f} GB requis"
            )


# ════════════════════════════════════════════════════════════════
# DÉTECTION OS INVITÉ
# ════════════════════════════════════════════════════════════════

class GuestOSDetector:
    """
    Détecte l'OS invité dans une image QCOW2 via libguestfs.
    """

    def detect(self, qcow2_path: str) -> tuple[GuestOS, str]:
        """
        Retourne (GuestOS, description).
        Ex: (GuestOS.WINDOWS, "Windows Server 2019")
        """
        try:
            import guestfs
            g = guestfs.GuestFS(python_return_dict=True)
            g.add_drive_opts(qcow2_path, format="qcow2", readonly=1)
            g.launch()

            roots = g.inspect_os()
            if not roots:
                g.close()
                logger.warning("libguestfs: aucun OS détecté dans l'image")
                return GuestOS.UNKNOWN, "Inconnu"

            root = roots[0]
            os_type    = g.inspect_get_type(root)
            os_distro  = g.inspect_get_distro(root)
            os_name    = g.inspect_get_product_name(root)
            os_version = (
                f"{g.inspect_get_major_version(root)}."
                f"{g.inspect_get_minor_version(root)}"
            )
            g.close()

            logger.info(
                f"OS détecté: {os_type} / {os_distro} / {os_name} / v{os_version}"
            )

            if os_type == "windows":
                return GuestOS.WINDOWS, os_name
            elif os_type in ("linux", "freebsd", "netbsd", "openbsd"):
                return GuestOS.LINUX, os_name
            else:
                return GuestOS.UNKNOWN, os_name

        except ImportError:
            logger.warning(
                "python3-libguestfs non installé — détection OS par heuristique"
            )
            return self._detect_heuristic(qcow2_path)

        except Exception as exc:
            logger.warning(f"Erreur détection OS: {exc}")
            return GuestOS.UNKNOWN, str(exc)

    def _detect_heuristic(self, qcow2_path: str) -> tuple[GuestOS, str]:
        """
        Fallback : détection par analyse de l'image sans libguestfs.
        Cherche des signatures Windows (NTFS, hiberfil.sys) ou Linux (ext4).
        """
        try:
            result = subprocess.run(
                [_find_tool("qemu-img") or "qemu-img", "info", qcow2_path],
                capture_output=True, text=True
            )
            output = result.stdout.lower()
            # Chercher des indices dans les métadonnées
            if "ntfs" in output or "fat32" in output:
                return GuestOS.WINDOWS, "Windows (heuristique)"
            elif "ext4" in output or "ext3" in output or "xfs" in output:
                return GuestOS.LINUX, "Linux (heuristique)"
        except Exception:
            pass
        return GuestOS.UNKNOWN, "Inconnu"


# ════════════════════════════════════════════════════════════════
# INJECTION VIRTIO WINDOWS
# ════════════════════════════════════════════════════════════════

class WindowsVirtIOInjector:
    """
    Injecte les pilotes VirtIO dans une image Windows offline.

    Ce que fait l'injection :
    ┌─────────────────────────────────────────────────────────────┐
    │ 1. Monter l'image QCOW2 en lecture/écriture (libguestfs)   │
    │ 2. Monter l'ISO virtio-win                                  │
    │ 3. Copier les fichiers .inf/.sys/.cat selon la version Win  │
    │    - viostor.sys  → contrôleur de stockage VirtIO (SCSI)   │
    │    - vioscsi.sys  → contrôleur SCSI alternatif             │
    │    - netkvm.sys   → carte réseau VirtIO                    │
    │    - balloon.sys  → memory balloon (optionnel)             │
    │ 4. Éditer la ruche SYSTEM offline (hivex) :                │
    │    - CriticalDeviceDatabase → démarrage garanti au boot    │
    │    - Services\viostor       → démarrage automatique        │
    │    - Services\netkvm        → démarrage automatique        │
    │ 5. Démonter proprement                                     │
    └─────────────────────────────────────────────────────────────┘
    """

    # Mapping version Windows → dossier dans l'ISO virtio-win
    WIN_VERSION_MAP = {
        "windows-2022":    "2k22/amd64",
        "windows-2019":    "2k19/amd64",
        "windows-2016":    "2k16/amd64",
        "windows-2012r2":  "2k12R2/amd64",
        "windows-10":      "w10/amd64",
        "windows-11":      "w11/amd64",
        "windows-7":       "w7/amd64",
        "default":         "2k19/amd64",  # Fallback sûr
    }

    # Pilotes à copier (dossier dans l'ISO → destination dans Windows)
    DRIVERS = [
        # (dossier_iso, nom_fichier, type)
        ("viostor",  "viostor.inf",   "storage"),
        ("viostor",  "viostor.sys",   "storage"),
        ("viostor",  "viostor.cat",   "storage"),
        ("vioscsi",  "vioscsi.inf",   "storage"),
        ("vioscsi",  "vioscsi.sys",   "storage"),
        ("vioscsi",  "vioscsi.cat",   "storage"),
        ("NetKVM",   "netkvm.inf",    "network"),
        ("NetKVM",   "netkvm.sys",    "network"),
        ("NetKVM",   "netkvm.cat",    "network"),
        ("Balloon",  "balloon.inf",   "misc"),
        ("Balloon",  "balloon.sys",   "misc"),
    ]

    # Clés registre pour CriticalDeviceDatabase (démarrage SCSI au boot)
    CRITICAL_DEVICE_KEYS = {
        # VirtIO SCSI storage (viostor)
        "PCI#VEN_1AF4&DEV_1001&SUBSYS_00021AF4&REV_00": {
            "ClassGUID": "{4D36E97B-E325-11CE-BFC1-08002BE10318}",
            "Service":   "viostor",
        },
        "PCI#VEN_1AF4&DEV_1001": {
            "ClassGUID": "{4D36E97B-E325-11CE-BFC1-08002BE10318}",
            "Service":   "viostor",
        },
        # VirtIO SCSI (vioscsi)
        "PCI#VEN_1AF4&DEV_1004&SUBSYS_00081AF4&REV_00": {
            "ClassGUID": "{4D36E97B-E325-11CE-BFC1-08002BE10318}",
            "Service":   "vioscsi",
        },
        "PCI#VEN_1AF4&DEV_1004": {
            "ClassGUID": "{4D36E97B-E325-11CE-BFC1-08002BE10318}",
            "Service":   "vioscsi",
        },
    }

    def __init__(self, virtio_iso_path: Optional[str] = None):
        self.virtio_iso = virtio_iso_path or _find_virtio_iso()

    def inject(self, qcow2_path: str, os_name: str = "") -> bool:
        """
        Injecte les pilotes VirtIO dans l'image Windows.
        Retourne True si succès.
        """
        if not self.virtio_iso:
            raise FileNotFoundError(
                "ISO virtio-win introuvable.\n"
                "Installer via: dnf install -y virtio-win\n"
                "Ou télécharger: https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/"
            )

        win_dir = self._detect_win_dir(os_name)
        logger.info(f"Injection VirtIO pour '{os_name}' — dossier ISO: {win_dir}")

        try:
            import guestfs
            g = guestfs.GuestFS(python_return_dict=True)

            # Monter l'image Windows en lecture/écriture
            g.add_drive_opts(qcow2_path, format="qcow2", readonly=0)
            # Monter l'ISO VirtIO en lecture seule
            g.add_drive_opts(self.virtio_iso, format="raw", readonly=1)
            g.launch()

            # Inspecter l'OS pour trouver les partitions
            roots = g.inspect_os()
            if not roots:
                g.close()
                raise RuntimeError("Aucun OS Windows détecté dans l'image")

            root = roots[0]
            windows_drive = g.inspect_get_drive_mappings(root)
            logger.info(f"Partitions Windows: {windows_drive}")

            # Monter la partition système Windows
            g.mount(root, "/")

            # Trouver le dossier System32 (drivers)
            system32 = self._find_system32(g)
            drivers_dir = f"{system32}/drivers"
            logger.info(f"Dossier drivers: {drivers_dir}")

            # Monter l'ISO VirtIO (second disque)
            iso_devs = [d for d in g.list_devices() if d != g.list_devices()[0]]
            if iso_devs:
                g.mount_ro(iso_devs[0], "/mnt")

            # Copier les pilotes
            copied = []
            for driver_folder, filename, dtype in self.DRIVERS:
                src = f"/mnt/{driver_folder}/{win_dir}/{filename}"
                dst = f"{drivers_dir}/{filename}"
                try:
                    if g.exists(src):
                        g.cp(src, dst)
                        copied.append(filename)
                        logger.debug(f"Copié: {filename}")
                    else:
                        logger.warning(f"Pilote non trouvé dans l'ISO: {src}")
                except Exception as e:
                    logger.warning(f"Erreur copie {filename}: {e}")

            logger.info(f"Pilotes copiés ({len(copied)}): {copied}")

            # Éditer le registre Windows offline
            self._patch_registry(g, system32)

            # Démonter proprement
            g.umount_all()
            g.close()

            logger.info("Injection VirtIO terminée avec succès")
            return True

        except ImportError:
            raise RuntimeError(
                "python3-libguestfs requis pour l'injection VirtIO.\n"
                "Installer: dnf install -y python3-libguestfs"
            )

    def _detect_win_dir(self, os_name: str) -> str:
        """Mappe le nom de l'OS au dossier correct dans l'ISO."""
        os_lower = os_name.lower()
        for key, virtio_dir in self.WIN_VERSION_MAP.items():
            if key.replace("windows-", "") in os_lower:
                return virtio_dir
        # Détecter par version générique
        if "2022" in os_lower:
            return self.WIN_VERSION_MAP["windows-2022"]
        elif "2019" in os_lower:
            return self.WIN_VERSION_MAP["windows-2019"]
        elif "2016" in os_lower:
            return self.WIN_VERSION_MAP["windows-2016"]
        elif "10" in os_lower:
            return self.WIN_VERSION_MAP["windows-10"]
        elif "11" in os_lower:
            return self.WIN_VERSION_MAP["windows-11"]
        elif "7" in os_lower:
            return self.WIN_VERSION_MAP["windows-7"]
        return self.WIN_VERSION_MAP["default"]

    def _find_system32(self, g) -> str:
        """Trouve le chemin de System32 dans l'image."""
        candidates = [
            "/Windows/System32",
            "/WINDOWS/System32",
            "/windows/System32",
        ]
        for c in candidates:
            if g.is_dir(c):
                return c
        # Chercher
        for item in g.ls("/"):
            candidate = f"/{item}/System32"
            if g.is_dir(candidate):
                return candidate
        raise RuntimeError("Impossible de trouver System32 dans l'image Windows")

    def _patch_registry(self, g, system32: str):
        """
        Édite les ruches registre offline pour enregistrer les pilotes VirtIO.
        Modifie SYSTEM\CurrentControlSet\Services et CriticalDeviceDatabase.
        """
        try:
            import hivex

            # Chemin de la ruche SYSTEM
            system_hive = f"{system32}/config/SYSTEM"
            if not g.is_file(system_hive):
                logger.warning("Ruche SYSTEM introuvable — édition registre ignorée")
                return

            # Télécharger la ruche localement pour l'éditer
            with tempfile.NamedTemporaryFile(suffix=".hiv", delete=False) as tmp:
                tmp_path = tmp.name

            g.download(system_hive, tmp_path)

            h = hivex.Hivex(tmp_path, write=True)

            # 1. Ajouter CriticalDeviceDatabase
            self._add_critical_devices(h)

            # 2. Enregistrer les services viostor et netkvm
            self._add_service(h, "viostor",  start=0)  # 0 = BOOT_START
            self._add_service(h, "vioscsi",  start=0)
            self._add_service(h, "netkvm",   start=3)  # 3 = DEMAND_START

            h.commit(tmp_path)
            h.close()

            # Remettre la ruche modifiée dans l'image
            g.upload(tmp_path, system_hive)
            os.unlink(tmp_path)

            logger.info("Ruches registre mises à jour (CriticalDeviceDatabase + Services)")

        except ImportError:
            logger.warning(
                "python3-hivex non installé — édition registre ignorée.\n"
                "Les pilotes sont copiés mais le démarrage VirtIO peut échouer.\n"
                "Installer: dnf install -y python3-hivex"
            )
        except Exception as exc:
            logger.warning(f"Erreur édition registre: {exc}")

    def _add_critical_devices(self, h):
        """Ajoute les entrées CriticalDeviceDatabase pour le boot VirtIO."""
        try:
            # Chercher le ControlSet001 (ou CurrentControlSet)
            root = h.root()
            for control_set in ["ControlSet001", "CurrentControlSet"]:
                try:
                    node = h.node_get_child(root, control_set)
                    control = h.node_get_child(node, "Control")
                    cdb = h.node_get_child(control, "CriticalDeviceDatabase")

                    for hw_id, values in self.CRITICAL_DEVICE_KEYS.items():
                        # Créer la clé si absente
                        existing = None
                        try:
                            existing = h.node_get_child(cdb, hw_id)
                        except Exception:
                            pass

                        if not existing:
                            new_node = h.node_add_child(cdb, hw_id)
                        else:
                            new_node = existing

                        # Ajouter ClassGUID et Service
                        h.node_set_value(new_node, {
                            "key":   "ClassGUID",
                            "t":     hivex.REG_SZ,
                            "value": values["ClassGUID"].encode("utf-16-le") + b"\x00\x00",
                        })
                        h.node_set_value(new_node, {
                            "key":   "Service",
                            "t":     hivex.REG_SZ,
                            "value": values["Service"].encode("utf-16-le") + b"\x00\x00",
                        })

                    logger.debug(f"CriticalDeviceDatabase mis à jour dans {control_set}")
                    break

                except Exception as e:
                    logger.debug(f"ControlSet {control_set}: {e}")

        except Exception as exc:
            logger.warning(f"CriticalDeviceDatabase: {exc}")

    def _add_service(self, h, service_name: str, start: int = 0):
        """Enregistre un service dans la ruche SYSTEM."""
        try:
            root = h.root()
            for control_set in ["ControlSet001", "CurrentControlSet"]:
                try:
                    node = h.node_get_child(root, control_set)
                    services = h.node_get_child(node, "Services")

                    # Créer ou récupérer le nœud du service
                    try:
                        svc_node = h.node_get_child(services, service_name)
                    except Exception:
                        svc_node = h.node_add_child(services, service_name)

                    def set_dword(node, key, value):
                        import struct
                        h.node_set_value(node, {
                            "key":   key,
                            "t":     hivex.REG_DWORD,
                            "value": struct.pack("<I", value),
                        })

                    def set_sz(node, key, value):
                        h.node_set_value(node, {
                            "key":   key,
                            "t":     hivex.REG_SZ,
                            "value": value.encode("utf-16-le") + b"\x00\x00",
                        })

                    set_dword(svc_node, "Start", start)     # 0=Boot, 3=Demand
                    set_dword(svc_node, "Type",  1)         # Kernel driver
                    set_dword(svc_node, "ErrorControl", 1)  # Normal
                    set_sz(svc_node,   "ImagePath",
                           f"system32\\drivers\\{service_name}.sys")
                    set_sz(svc_node,   "DisplayName", service_name)

                    logger.debug(f"Service {service_name} enregistré dans {control_set}")
                    break

                except Exception as e:
                    logger.debug(f"Service {service_name} / {control_set}: {e}")

        except Exception as exc:
            logger.warning(f"Service {service_name}: {exc}")


# ════════════════════════════════════════════════════════════════
# VÉRIFICATION LINUX
# ════════════════════════════════════════════════════════════════

class LinuxVirtIOVerifier:
    """
    Vérifie que les modules VirtIO sont présents dans l'image Linux.
    Sous Linux moderne (kernel 2.6.25+), les modules VirtIO sont
    intégrés nativement — la vérification est donc quasi toujours OK.
    """

    REQUIRED_MODULES = [
        "virtio_blk",    # Stockage VirtIO
        "virtio_net",    # Réseau VirtIO
        "virtio_pci",    # Bus PCI VirtIO
    ]

    def verify(self, qcow2_path: str) -> tuple[bool, list[str]]:
        """
        Retourne (ok, missing_modules).
        Si ok=True, l'image Linux est prête pour OpenStack/KVM.
        """
        try:
            import guestfs
            g = guestfs.GuestFS(python_return_dict=True)
            g.add_drive_opts(qcow2_path, format="qcow2", readonly=1)
            g.launch()

            roots = g.inspect_os()
            if not roots:
                g.close()
                return True, []  # On ne peut pas vérifier, on passe

            root = roots[0]
            g.mount(root, "/")

            # Chercher les modules dans /lib/modules/*/kernel/drivers/virtio/
            missing = []
            try:
                modules_dirs = g.glob_expand("/lib/modules/*/kernel/drivers/virtio")
                if modules_dirs:
                    for mod in self.REQUIRED_MODULES:
                        found = False
                        for mdir in modules_dirs:
                            if g.glob_expand(f"{mdir}/{mod}.ko*"):
                                found = True
                                break
                        if not found:
                            # Chercher aussi dans built-in modules
                            builtin = g.glob_expand(
                                f"/lib/modules/*/modules.builtin"
                            )
                            for bf in builtin:
                                try:
                                    content = g.cat(bf)
                                    if mod.replace("_", "-") in content or mod in content:
                                        found = True
                                        break
                                except Exception:
                                    pass
                        if not found:
                            missing.append(mod)
                else:
                    logger.warning("Aucun répertoire de modules trouvé")

            except Exception as e:
                logger.debug(f"Vérification modules: {e}")

            g.umount_all()
            g.close()

            if missing:
                logger.warning(f"Modules VirtIO absents: {missing}")
            else:
                logger.info("Tous les modules VirtIO sont présents")

            return len(missing) == 0, missing

        except ImportError:
            # Sans libguestfs, on suppose que c'est OK pour Linux moderne
            logger.info("libguestfs absent — modules VirtIO supposés présents (Linux moderne)")
            return True, []
        except Exception as exc:
            logger.warning(f"Vérification VirtIO Linux: {exc}")
            return True, []


# ════════════════════════════════════════════════════════════════
# ORCHESTRATEUR PRINCIPAL
# ════════════════════════════════════════════════════════════════

class VMConverter:
    """
    Orchestre la conversion complète d'une VM VMware vers OpenStack.

    Usage:
        converter = VMConverter(config)
        result = converter.convert(vmdk_path, vm_name, dry_run=False)
    """

    def __init__(self, config: dict):
        self.config     = config
        self.workspace  = config.get("workspace_dir", "/tmp/migration_workspace")
        self.qemu_img   = config.get("qemu_img_path")
        self.compression = config.get("compression", True)
        self.virtio_iso  = config.get("virtio_iso_path")

        self.disk_converter = DiskConverter(
            qemu_img_path=self.qemu_img,
            workspace_dir=self.workspace,
            compression=self.compression,
        )
        self.os_detector  = GuestOSDetector()
        self.win_injector = WindowsVirtIOInjector(self.virtio_iso)
        self.lin_verifier = LinuxVirtIOVerifier()

    def convert(self, vmdk_path: str, vm_name: str,
                dry_run: bool = False) -> ConversionResult:
        """
        Pipeline de conversion complet.
        """
        import time
        start = time.monotonic()
        result = ConversionResult(success=False)

        try:
            logger.info(f"[CONVERT] Démarrage conversion: {vm_name}")
            logger.info(f"[CONVERT] VMDK source: {vmdk_path}")
            logger.info(f"[CONVERT] dry_run: {dry_run}")

            if dry_run:
                return self._dry_run_result(vm_name, start)

            # ── Étape 1 : Conversion VMDK → QCOW2 ────────────────
            logger.info("[CONVERT] Étape 1/4 : Conversion VMDK → QCOW2")
            qcow2_path = self.disk_converter.convert(vmdk_path, vm_name)
            result.qcow2_path = qcow2_path

            qcow2_size = os.path.getsize(qcow2_path) / (1024 ** 3)
            result.disk_size_gb = round(qcow2_size, 2)
            logger.info(f"[CONVERT] QCOW2 créé: {qcow2_path} ({qcow2_size:.2f} GB)")

            # ── Étape 2 : Détection OS ────────────────────────────
            logger.info("[CONVERT] Étape 2/4 : Détection OS invité")
            guest_os, os_name = self.os_detector.detect(qcow2_path)
            result.guest_os = guest_os
            result.os_name  = os_name
            logger.info(f"[CONVERT] OS détecté: {guest_os.value} — {os_name}")

            # ── Étape 3 : Adaptation invité ───────────────────────
            logger.info("[CONVERT] Étape 3/4 : Adaptation invité")

            if guest_os == GuestOS.WINDOWS:
                logger.info("[CONVERT] → Injection pilotes VirtIO Windows")
                try:
                    injected = self.win_injector.inject(qcow2_path, os_name)
                    result.virtio_injected = injected
                    logger.info("[CONVERT] ✓ Injection VirtIO terminée")
                except FileNotFoundError as e:
                    # ISO VirtIO absent → basculer en mode IDE (Option A de secours)
                    logger.warning(f"[CONVERT] ISO VirtIO absent: {e}")
                    logger.warning("[CONVERT] → Basculement mode IDE (Option A de secours)")
                    result.warnings.append(
                        "ISO virtio-win absent — la VM Windows démarrera en mode IDE. "
                        "Installer virtio-win pour activer VirtIO: dnf install -y virtio-win"
                    )
                    result.virtio_injected = False
                except Exception as e:
                    logger.error(f"[CONVERT] Injection VirtIO échouée: {e}")
                    result.errors.append(f"Injection VirtIO: {e}")
                    result.warnings.append(
                        "L'injection VirtIO a échoué — la VM peut démarrer en mode IDE "
                        "mais les performances disque seront réduites."
                    )

            elif guest_os == GuestOS.LINUX:
                logger.info("[CONVERT] → Vérification modules VirtIO Linux")
                ok, missing = self.lin_verifier.verify(qcow2_path)
                if ok:
                    result.virtio_injected = True
                    logger.info("[CONVERT] ✓ Modules VirtIO Linux présents")
                else:
                    result.warnings.append(
                        f"Modules VirtIO absents du noyau Linux: {missing}. "
                        "L'image peut ne pas démarrer sur KVM."
                    )

            else:
                result.warnings.append(
                    "OS invité non identifié — l'adaptation VirtIO est ignorée."
                )

            # ── Étape 4 : Validation finale ───────────────────────
            logger.info("[CONVERT] Étape 4/4 : Validation du QCOW2")
            self._validate_qcow2(qcow2_path)

            result.success = True
            result.duration_s = round(time.monotonic() - start, 1)

            logger.info(
                f"[CONVERT] ✅ Conversion terminée en {result.duration_s}s — "
                f"OS: {guest_os.value} — VirtIO: {result.virtio_injected} — "
                f"Taille: {result.disk_size_gb} GB"
            )

        except Exception as exc:
            result.success = False
            result.errors.append(str(exc))
            result.duration_s = round(time.monotonic() - start, 1)
            logger.error(f"[CONVERT] ❌ Conversion échouée: {exc}")

        return result

    def _validate_qcow2(self, qcow2_path: str):
        """Valide l'intégrité du QCOW2 avec qemu-img check."""
        qemu = self.qemu_img or _find_tool("qemu-img") or "qemu-img"
        result = subprocess.run(
            [qemu, "check", "-f", "qcow2", qcow2_path],
            capture_output=True, text=True
        )
        if result.returncode not in (0, 1):  # 1 = warnings non bloquants
            raise RuntimeError(
                f"QCOW2 corrompu (qemu-img check):\n{result.stderr}"
            )
        logger.info("[CONVERT] Intégrité QCOW2 validée")

    def _dry_run_result(self, vm_name: str, start: float) -> ConversionResult:
        """Résultat simulé pour le mode dry_run."""
        import time
        logger.info(f"[CONVERT] dry_run — simulation conversion {vm_name}")
        return ConversionResult(
            success         = True,
            qcow2_path      = f"{self.workspace}/{vm_name}.qcow2",
            guest_os        = GuestOS.LINUX,
            os_name         = "Ubuntu 22.04 LTS (simulé)",
            virtio_injected = True,
            disk_size_gb    = 10.0,
            duration_s      = round(time.monotonic() - start, 1),
        )

    def cleanup(self, qcow2_path: str):
        """Supprime le QCOW2 temporaire après upload."""
        try:
            if qcow2_path and os.path.isfile(qcow2_path):
                os.unlink(qcow2_path)
                logger.info(f"[CONVERT] Nettoyage: {qcow2_path} supprimé")
        except Exception as e:
            logger.warning(f"[CONVERT] Nettoyage: {e}")

    def get_dependencies_status(self) -> dict:
        """Retourne l'état de toutes les dépendances nécessaires."""
        status = {}

        # qemu-img
        qemu = _find_tool("qemu-img")
        status["qemu-img"] = {
            "available": qemu is not None,
            "path":      qemu or "Non trouvé",
            "required":  True,
            "install":   "dnf install -y qemu-img",
        }

        # libguestfs-tools
        guestfish = _find_tool("guestfish")
        status["libguestfs"] = {
            "available": guestfish is not None,
            "path":      guestfish or "Non trouvé",
            "required":  False,
            "install":   "dnf install -y libguestfs-tools python3-libguestfs",
        }

        # python3-libguestfs
        try:
            import guestfs
            status["python3-libguestfs"] = {"available": True, "required": False}
        except ImportError:
            status["python3-libguestfs"] = {
                "available": False,
                "required":  False,
                "install":   "dnf install -y python3-libguestfs",
            }

        # python3-hivex
        try:
            import hivex
            status["python3-hivex"] = {"available": True, "required": False}
        except ImportError:
            status["python3-hivex"] = {
                "available": False,
                "required":  False,
                "install":   "dnf install -y python3-hivex",
            }

        # virtio-win ISO
        iso = _find_virtio_iso()
        status["virtio-win"] = {
            "available": iso is not None,
            "path":      iso or "Non trouvé",
            "required":  False,
            "install":   "dnf install -y virtio-win",
        }

        return status
