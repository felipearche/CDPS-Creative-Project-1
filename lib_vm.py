"""Gestión de máquinas KVM y redes OVS para la Creativa 1 de CDPS."""

import copy
import logging
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET


log = logging.getLogger("auto-p2")
WORKDIR = Path.cwd()
TEMPLATE = WORKDIR / "plantilla-vm-pc1.xml"


class ErrorEscenario(RuntimeError):
    """Error de configuración o ejecución del escenario."""


def ejecutar(argumentos, *, cwd=None, comprobar=True):
    """Ejecuta un comando sin shell, registrando su ejecución."""
    args = [str(arg) for arg in argumentos]
    log.debug("Ejecutando: %s", " ".join(args))
    try:
        resultado = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ErrorEscenario(f"No se encuentra el comando: {args[0]}") from exc

    if resultado.stdout.strip():
        log.debug("stdout: %s", resultado.stdout.strip())
    if resultado.stderr.strip():
        log.debug("stderr: %s", resultado.stderr.strip())
    if comprobar and resultado.returncode != 0:
        mensaje = resultado.stderr.strip() or resultado.stdout.strip()
        raise ErrorEscenario(f"Error ejecutando {' '.join(args)}: {mensaje}")
    return resultado


class NET:
    """Un bridge Open vSwitch (RED1, RED2 o RED3)."""

    def __init__(self, name):
        self.name = name

    def exists(self):
        resultado = ejecutar(
            ["sudo", "ovs-vsctl", "br-exists", self.name],
            comprobar=False
        )
        if resultado.returncode == 0:
            return True
        if resultado.returncode == 2:
            return False
        detalle = resultado.stderr.strip() or resultado.stdout.strip()
        raise ErrorEscenario(
            f"No se pudo consultar el bridge {self.name}: {detalle}"
        )

    def create_net(self):
        """Crea el bridge y devuelve True si era nuevo."""
        if self.exists():
            log.info("El bridge %s ya existía; se reutiliza", self.name)
            return False
        ejecutar(["sudo", "ovs-vsctl", "add-br", self.name])
        log.info("Bridge creado: %s", self.name)
        return True

    def destroy_net(self):
        if self.exists():
            ejecutar(["sudo", "ovs-vsctl", "del-br", self.name])
            log.info("Bridge eliminado: %s", self.name)


class VM:
    """Una máquina virtual persistente gestionada por libvirt."""

    def __init__(self, name):
        self.name = name
        self.xml = WORKDIR / f"{name}.xml"
        self.disk = WORKDIR / f"{name}.qcow2"
        self.iso = WORKDIR / f"{name}-cdata.iso"
        self.cloud_dir = WORKDIR / ".cloud-init" / name

    def is_defined(self):
        """Comprueba si la VM existe, notificando errores de libvirt."""
        resultado = ejecutar(
            ["sudo", "virsh", "list", "--all", "--name"]
        )
        return self.name in (
            line.strip() for line in resultado.stdout.splitlines()
        )

    def state(self):
        """Estado normalizado: libvirt puede devolverlo en español o inglés."""
        resultado = ejecutar(
            ["sudo", "virsh", "domstate", self.name], comprobar=False
        )
        if resultado.returncode != 0:
            if self.is_defined():
                detalle = resultado.stderr.strip() or resultado.stdout.strip()
                raise ErrorEscenario(
                    f"No se pudo consultar el estado de {self.name}: {detalle}"
                )
            return None
        estado = resultado.stdout.strip().lower()
        equivalencias = {
            "ejecutando": "running",
            "en ejecución": "running",
            "apagado": "shut off",
            "apagada": "shut off",
            "pausado": "paused",
            "en pausa": "paused",
        }
        return equivalencias.get(estado, estado)

    def _cloud_init(self, interfaces, forwarding):
        """Escribe los tres archivos NoCloud y crea su ISO de configuración."""
        self.cloud_dir.mkdir(parents=True, exist_ok=True)
        (self.cloud_dir / "meta-data").write_text(
            f"instance-id: {self.name}-id\nlocal-hostname: {self.name}\n",
            encoding="utf-8",
        )

        lines = ["version: 2", "ethernets:"]
        for index, iface in enumerate(interfaces):
            lines.extend([
                f"  eth{index}:",
                "    dhcp4: false",
                "    dhcp6: false",
                "    link-local: []",
                "    addresses:",
                f"      - {iface['addr']}",
            ])
            routes = iface.get("routes", [])
            if routes:
                lines.append("    routes:")
                for route in routes:
                    lines.extend([
                        f"      - to: {route['to']}",
                        f"        via: {route['via']}",
                    ])
        (self.cloud_dir / "network-config").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

        # La imagen Ubuntu del laboratorio no siempre instala al arrancar
        # las rutas que netplan ha generado correctamente. Reaplicamos netplan
        # tras arrancar systemd-networkd, también en reinicios posteriores.
        # cloud-init runcmd se ejecuta solo durante el primer arranque.
        user_lines = [
            "#cloud-config",
            f"hostname: {self.name}",
            "manage_etc_hosts: true",
            "write_files:",
            "  - path: /etc/systemd/system/cdps-netplan-apply.service",
            "    permissions: '0644'",
            "    content: |",
            "      [Unit]",
            "      Description=CDPS reapply Netplan after boot",
            "      After=systemd-networkd.service",
            "      Wants=systemd-networkd.service",
            "      [Service]",
            "      Type=oneshot",
            "      ExecStart=/usr/sbin/netplan apply",
            "      [Install]",
            "      WantedBy=multi-user.target",
        ]
        if forwarding:
            user_lines.extend([
                "  - path: /etc/sysctl.d/99-cdps-ipforward.conf",
                "    permissions: '0644'",
                "    content: |",
                "      net.ipv4.ip_forward=1",
            ])
        user_lines.extend([
            "runcmd:",
            "  - [systemctl, daemon-reload]",
            "  - [systemctl, enable, cdps-netplan-apply.service]",
            "  - [netplan, apply]",
        ])
        if forwarding:
            user_lines.append("  - [sysctl, -p, /etc/sysctl.d/99-cdps-ipforward.conf]")
        (self.cloud_dir / "user-data").write_text(
            "\n".join(user_lines) + "\n", encoding="utf-8"
        )

        ejecutar([
            "genisoimage", "-quiet", "-output", self.iso,
            "-volid", "cidata", "-joliet", "-rock",
            "user-data", "meta-data", "network-config",
        ], cwd=self.cloud_dir)

    def define_vm(self, image, interfaces, forwarding=False):
        """Genera disco COW, cloud-init, XML y define la VM con virsh."""
        if self.is_defined():
            raise ErrorEscenario(f"La VM {self.name} ya está definida en libvirt")
        for path in (self.disk, self.iso, self.xml):
            if path.exists():
                raise ErrorEscenario(f"El fichero ya existe: {path}")
        if not interfaces:
            raise ErrorEscenario(f"La VM {self.name} necesita al menos una interfaz")
        if not TEMPLATE.is_file():
            raise ErrorEscenario(f"Falta plantilla XML: {TEMPLATE}")

        ejecutar([
            "qemu-img", "create", "-F", "qcow2", "-f", "qcow2",
            "-b", Path(image).resolve(), self.disk,
        ])
        self._cloud_init(interfaces, forwarding)

        try:
            tree = ET.parse(TEMPLATE)
        except ET.ParseError as exc:
            raise ErrorEscenario(
                f"Plantilla XML inválida: {exc}"
            ) from exc
        root = tree.getroot()
        name_element = root.find("name")
        disk_source = root.find("./devices/disk[@device='disk']/source")
        iso_source = root.find("./devices/disk[@device='cdrom']/source")
        devices = root.find("devices")
        original_iface = root.find("./devices/interface")
        if any(x is None for x in (name_element, disk_source, iso_source, devices, original_iface)):
            raise ErrorEscenario("La plantilla XML no tiene la estructura esperada")

        name_element.text = self.name
        disk_source.set("file", str(self.disk))
        iso_source.set("file", str(self.iso))

        first_pos = list(devices).index(original_iface)
        for old_iface in list(devices.findall("interface")):
            devices.remove(old_iface)
        for index, iface in enumerate(interfaces):
            new_iface = copy.deepcopy(original_iface)
            source = new_iface.find("source")
            if source is None:
                raise ErrorEscenario("La interfaz de la plantilla no tiene <source>")
            source.set("bridge", iface["bridge"])
            # Sin MAC fija: libvirt asignará una MAC distinta por interfaz.
            mac = new_iface.find("mac")
            if mac is not None:
                new_iface.remove(mac)
            devices.insert(first_pos + index, new_iface)

        ET.indent(tree, space="  ")
        tree.write(self.xml, encoding="unicode", xml_declaration=True)
        ejecutar(["sudo", "virsh", "define", self.xml])
        log.info("VM definida: %s", self.name)

    def start_vm(self):
        if not self.is_defined():
            raise ErrorEscenario(f"VM no definida: {self.name}")
        if self.state() == "running":
            log.info("VM ya arrancada: %s", self.name)
            return
        ejecutar(["sudo", "virsh", "start", self.name])
        log.info("VM arrancada: %s", self.name)

    def show_console_vm(self):
        """Abre una consola independiente sin bloquear el script."""
        if shutil.which("gnome-terminal"):
            cmd = ["gnome-terminal", "--title", self.name, "--", "sudo", "virsh", "console", self.name]
        elif shutil.which("xfce4-terminal"):
            cmd = [
                "xfce4-terminal", "-T", self.name, "--hide-menubar",
                "-e", f"sudo virsh console {self.name}",
            ]
        elif shutil.which("xterm"):
            cmd = ["xterm", "-T", self.name, "-e", "sudo", "virsh", "console", self.name]
        elif shutil.which("virt-viewer"):
            cmd = ["virt-viewer", self.name]
        else:
            raise ErrorEscenario("No hay terminal gráfico para abrir las consolas")
        log.debug("Abriendo consola: %s", self.name)
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            raise ErrorEscenario(f"No se puede abrir consola de {self.name}: {exc}") from exc

    def stop_vm(self):
        estado = self.state()
        if estado is None or estado == "shut off":
            log.info("VM ya apagada o no definida: %s", self.name)
            return
        if estado == "running":
            ejecutar(["sudo", "virsh", "shutdown", self.name])
            log.info("Solicitado apagado ordenado de %s", self.name)
        else:
            log.info("Estado de %s: %s", self.name, estado)

    def undefine_vm(self):
        if not self.is_defined():
            return
        if self.state() != "shut off":
            raise ErrorEscenario(f"No se libera {self.name}: primero debe estar apagada")
        ejecutar(["sudo", "virsh", "undefine", self.name])
        log.info("VM liberada: %s", self.name)

    def remove_files(self):
        for path in (self.disk, self.xml, self.iso):
            if path.exists():
                path.unlink()
                log.debug("Eliminado: %s", path)
        if self.cloud_dir.exists():
            shutil.rmtree(self.cloud_dir)

# Alias descriptivo: el enunciado habla de una clase Red;
# el esqueleto oficial la llama NET.
Red = NET
