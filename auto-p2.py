#!/usr/bin/env python3
"""Creativa 1 de CDPS: define, start, stop y undefine del escenario KVM."""

import argparse
import json
import logging
from pathlib import Path
import sys
import time

from lib_vm import ErrorEscenario, NET, VM, ejecutar


LOG = logging.getLogger("auto-p2")
ROOT = Path.cwd()
CONFIG = ROOT / "auto-p2.json"
STATE = ROOT / ".auto-p2-state.json"
BASE = ROOT / "cdps-vm-base-pc1.qcow2"
TEMPLATE = ROOT / "plantilla-vm-pc1.xml"
NETWORKS = ("RED1", "RED2", "RED3")
HOST_ROUTES = ("10.0.10.128/26", "10.0.10.64/26")
HOST_ADDRESS = "10.0.10.3/26"
HOST_GATEWAY = "10.0.10.1"


def read_config():
    if not CONFIG.is_file():
        raise ErrorEscenario(f"Falta {CONFIG.name}")
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ErrorEscenario(f"JSON no válido: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ErrorEscenario("auto-p2.json debe contener un objeto JSON")
    count = cfg.get("num_servers")
    if type(count) is not int or not 1 <= count <= 5:
        raise ErrorEscenario("num_servers debe ser un entero de 1 a 5")
    if type(cfg.get("debug", False)) is not bool:
        raise ErrorEscenario("debug debe ser true o false")
    return cfg


def initialize_logging(debug):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )


def specifications(num_servers):
    """Describe la topología de la figura 1 del enunciado."""
    def iface(bridge, address, routes=()):
        return {"bridge": bridge, "addr": address, "routes": list(routes)}

    def route(destination, via):
        return {"to": destination, "via": via}

    specs = [
        ("c1", [iface("RED1", "10.0.10.2/26", [route("default", "10.0.10.1")])], False),
        ("fw", [
            iface("RED1", "10.0.10.1/26"),
            iface("RED2", "10.0.10.129/26", [route("10.0.10.64/26", "10.0.10.130")]),
        ], True),
        ("lb", [
            iface("RED2", "10.0.10.130/26", [route("default", "10.0.10.129")]),
            iface("RED3", "10.0.10.65/26"),
        ], True),
    ]
    for n in range(1, num_servers + 1):
        specs.append((
            f"s{n}",
            [iface("RED3", f"10.0.10.{65+n}/26", [route("default", "10.0.10.65")])],
            False,
        ))
    return specs


def save_state(state):
    """Escribe el estado de forma atómica para evitar archivos truncados."""
    temporary = STATE.with_name(STATE.name + ".tmp")
    try:
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(STATE)
    except OSError as exc:
        raise ErrorEscenario(f"No se puede guardar el estado: {exc}") from exc


def load_state():
    """Valida el inventario antes de operar sobre VMs y archivos locales."""
    if not STATE.is_file():
        raise ErrorEscenario("Escenario no definido por auto-p2.py; ejecuta 'define'")
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ErrorEscenario(f"No se puede leer el estado: {exc}") from exc
    if not isinstance(state, dict) or state.get("created_by") != "auto-p2.py":
        raise ErrorEscenario("Archivo de estado no reconocido")

    vms = state.get("vms")
    if not isinstance(vms, list) or not 4 <= len(vms) <= 8:
        raise ErrorEscenario("Inventario de VMs inválido en el archivo de estado")
    expected = ["c1", "fw", "lb"] + [f"s{i}" for i in range(1, len(vms) - 2)]
    if vms != expected:
        raise ErrorEscenario("Nombres u orden de VMs inválidos en el archivo de estado")

    bridges = state.get("bridges_added")
    routes = state.get("host_routes_added")
    if (not isinstance(bridges, list) or len(bridges) != len(set(map(str, bridges)))
            or not all(isinstance(x, str) and x in NETWORKS for x in bridges)):
        raise ErrorEscenario("Bridges inválidos en el archivo de estado")
    if (not isinstance(routes, list) or len(routes) != len(set(map(str, routes)))
            or not all(isinstance(x, str) and x in HOST_ROUTES for x in routes)):
        raise ErrorEscenario("Rutas inválidas en el archivo de estado")
    if type(state.get("host_address_added")) is not bool:
        raise ErrorEscenario("Dirección del host inválida en el archivo de estado")
    return state


def host_network(state):
    """Configura la interfaz RED1 del host y las rutas a RED2/RED3."""
    ejecutar(["sudo", "ip", "link", "set", "dev", "RED1", "up"])
    addresses = ejecutar(["ip", "-4", "-o", "addr", "show", "dev", "RED1"]).stdout
    if HOST_ADDRESS not in addresses:
        state["host_address_added"] = True
        save_state(state)
        ejecutar(["sudo", "ip", "addr", "add", HOST_ADDRESS, "dev", "RED1"])
    for network in HOST_ROUTES:
        existing = ejecutar(["ip", "-4", "route", "show", network]).stdout.strip()
        if not existing:
            if network not in state["host_routes_added"]:
                state["host_routes_added"].append(network)
                save_state(state)
            ejecutar(["sudo", "ip", "route", "add", network,
                      "via", HOST_GATEWAY, "dev", "RED1"])
        elif f"via {HOST_GATEWAY}" not in existing or "dev RED1" not in existing:
            raise ErrorEscenario(f"Existe una ruta conflictiva hacia {network}: {existing}")
    LOG.info("Host configurado en RED1 (%s)", HOST_ADDRESS)


def define(num_servers):
    if STATE.exists():
        raise ErrorEscenario("Ya existe el escenario; usa 'undefine' antes de redefinirlo")
    if not BASE.is_file() or not TEMPLATE.is_file():
        raise ErrorEscenario("Faltan la imagen base o la plantilla XML de PC1")

    specs = specifications(num_servers)
    for name, _, _ in specs:
        vm = VM(name)
        if vm.is_defined():
            raise ErrorEscenario(f"Ya existe la VM {name} en libvirt (¿prueba manual?)")
        for path in (vm.xml, vm.disk, vm.iso):
            if path.exists():
                raise ErrorEscenario(f"Existe un fichero que usaría el script: {path}")

    state = {
        "created_by": "auto-p2.py",
        "vms": [name for name, _, _ in specs],
        "bridges_added": [],
        "host_address_added": False,
        "host_routes_added": [],
    }
    save_state(state)
    LOG.info("Definiendo escenario con %s servidores", num_servers)
    try:
        for name in NETWORKS:
            net = NET(name)
            if net.exists():
                LOG.info("El bridge %s ya existía; se reutiliza", name)
                continue
            state["bridges_added"].append(name)
            save_state(state)
            if not net.create_net():
                state["bridges_added"].remove(name)
                save_state(state)
        host_network(state)
        for name, interfaces, forwarding in specs:
            VM(name).define_vm(BASE, interfaces, forwarding=forwarding)
    except (ErrorEscenario, OSError) as exc:
        LOG.error("Fallo durante define; intentando deshacer los cambios parciales")
        try:
            undefine()
        except (ErrorEscenario, OSError) as cleanup_error:
            raise ErrorEscenario(
                f"Falló define ({exc}) y la limpieza parcial ({cleanup_error}). "
                "Conservamos el estado para reintentar 'undefine'."
            ) from exc
        raise
    LOG.info("Escenario definido. Ahora ejecuta 'python3 auto-p2.py start'")


def start():
    state = load_state()
    for name in NETWORKS:
        net = NET(name)
        if net.exists():
            continue
        already_tracked = name in state["bridges_added"]
        if not already_tracked:
            state["bridges_added"].append(name)
            save_state(state)
        if not net.create_net() and not already_tracked:
            state["bridges_added"].remove(name)
            save_state(state)
    host_network(state)
    # Los routers se arrancan primero, pero el script no espera a que completen cloud-init.
    order = [n for n in ("fw", "lb", "c1") if n in state["vms"]]
    order += [n for n in state["vms"] if n.startswith("s")]
    for name in order:
        VM(name).start_vm()
    for name in order:
        VM(name).show_console_vm()
    LOG.info("VMs arrancadas; espera a que cloud-init termine en cada una")


def shutdown_all(names):
    for name in names:
        VM(name).stop_vm()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        remaining = [name for name in names
                     if VM(name).state() not in (None, "shut off")]
        if not remaining:
            return
        time.sleep(2)
    raise ErrorEscenario(
        "Las VMs no se han apagado dentro del plazo: " + ", ".join(remaining)
        + ". Comprueba 'sudo virsh list --all'; no se ha forzado su apagado."
    )


def stop():
    state = load_state()
    LOG.info("Solicitando apagado ordenado sin borrar discos")
    shutdown_all(state["vms"])
    LOG.info("Todas las VMs están paradas. Los datos permanecen en los QCOW2")


def undefine():
    state = load_state()
    LOG.info("Liberando escenario (con apagado ordenado previo)")
    shutdown_all(state["vms"])
    for name in reversed(state["vms"]):
        VM(name).undefine_vm()
    for name in state["vms"]:
        VM(name).remove_files()
    cloud_root = ROOT / ".cloud-init"
    if cloud_root.exists() and not any(cloud_root.iterdir()):
        cloud_root.rmdir()

    # Eliminar solo los recursos del host creados por este escenario.
    # Si ya no existen, se permite continuar; otros fallos se notifican.
    for network in state["host_routes_added"]:
        result = ejecutar(
            ["sudo", "ip", "route", "del", network,
             "via", HOST_GATEWAY, "dev", "RED1"],
            comprobar=False,
        )
        if result.returncode != 0:
            remaining = ejecutar(
                ["ip", "-4", "route", "show", network]
            ).stdout.strip()
            if remaining:
                raise ErrorEscenario(
                    f"No se pudo eliminar la ruta {network}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            LOG.warning("Ruta ya ausente: %s", network)

    if state["host_address_added"]:
        result = ejecutar(
            ["sudo", "ip", "addr", "del", HOST_ADDRESS,
             "dev", "RED1"],
            comprobar=False,
        )
        if result.returncode != 0:
            addresses = ejecutar(
                ["ip", "-4", "-o", "addr", "show"]
            ).stdout.splitlines()
            still_present = any(
                line.split()[1].split("@")[0] == "RED1"
                and f"inet {HOST_ADDRESS}" in line
                for line in addresses
            )
            if still_present:
                raise ErrorEscenario(
                    f"No se pudo eliminar la dirección {HOST_ADDRESS}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            LOG.warning("Dirección del host ya ausente: %s", HOST_ADDRESS)
    for name in reversed(state["bridges_added"]):
        NET(name).destroy_net()
    STATE.unlink()
    STATE.with_name(STATE.name + ".tmp").unlink(missing_ok=True)
    LOG.info("Escenario liberado; se conservan la imagen base, XML plantilla y scripts")


def main():
    parser = argparse.ArgumentParser(description="CDPS Creativa 1: gestión de escenario KVM")
    parser.add_argument("orden", choices=["define", "start", "stop", "undefine"])
    args = parser.parse_args()
    try:
        # Las órdenes de limpieza dependen del estado guardado, no del JSON.
        # Así se puede recuperar el laboratorio incluso si el JSON está dañado.
        if args.orden in ("stop", "undefine"):
            try:
                cfg = read_config()
            except ErrorEscenario as config_error:
                initialize_logging(False)
                LOG.warning("Configuración inválida, se usa el estado para limpiar: %s", config_error)
            else:
                initialize_logging(cfg.get("debug", False))
        else:
            cfg = read_config()
            initialize_logging(cfg.get("debug", False))

        if args.orden == "define":
            define(cfg["num_servers"])
        else:
            {"start": start, "stop": stop, "undefine": undefine}[args.orden]()
    except (ErrorEscenario, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
