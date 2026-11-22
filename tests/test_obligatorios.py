"""Pruebas offline de la Creativa 1. No ejecutan virsh, sudo, OVS ni KVM.

Ejecutar desde la raíz: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]


class ObligatoriosSinKvm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cdps_pc1_test_")
        self.work = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.work)
        for filename in ("auto-p2.py", "lib_vm.py", "plantilla-vm-pc1.xml", "auto-p2.json"):
            shutil.copy2(SOURCE / filename, self.work / filename)
        (self.work / "cdps-vm-base-pc1.qcow2").touch()
        sys.path.insert(0, str(self.work))
        sys.modules.pop("lib_vm", None)
        import lib_vm
        self.lib = lib_vm
        spec = importlib.util.spec_from_file_location("cdps_auto_p2_for_test", self.work / "auto-p2.py")
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)

    def tearDown(self):
        os.chdir(self.old_cwd)
        sys.path = [entry for entry in sys.path if entry != str(self.work)]
        sys.modules.pop("lib_vm", None)
        self.tmp.cleanup()

    def valid_state(self, servers=1):
        return {
            "created_by": "auto-p2.py",
            "vms": ["c1", "fw", "lb"] + [f"s{i}" for i in range(1, servers + 1)],
            "bridges_added": ["RED1", "RED2", "RED3"],
            "host_address_added": True,
            "host_routes_added": ["10.0.10.128/26", "10.0.10.64/26"],
        }

    def test_topologia_1_a_5(self):
        for n in range(1, 6):
            with self.subTest(n=n):
                specs = self.app.specifications(n)
                self.assertEqual([name for name, _, _ in specs], self.valid_state(n)["vms"])
                self.assertEqual(specs[0][1][0]["addr"], "10.0.10.2/26")
                self.assertEqual(specs[1][1][1]["addr"], "10.0.10.129/26")
                self.assertEqual(specs[2][1][1]["addr"], "10.0.10.65/26")
                for i in range(n):
                    self.assertEqual(specs[3 + i][1][0]["addr"], f"10.0.10.{66+i}/26")

    def test_json_limites(self):
        for n in range(1, 6):
            self.app.CONFIG.write_text(json.dumps({"num_servers": n}))
            self.assertEqual(self.app.read_config()["num_servers"], n)
        for bad in (0, 6, -1, True, 1.5, "3", None, {}, []):
            with self.subTest(value=repr(bad)):
                self.app.CONFIG.write_text(json.dumps({"num_servers": bad}))
                with self.assertRaises(self.lib.ErrorEscenario):
                    self.app.read_config()

    def test_estado_atómico_y_validado(self):
        state = self.valid_state(3)
        self.app.save_state(state)
        self.assertEqual(self.app.load_state(), state)
        self.assertFalse((self.work / ".auto-p2-state.json.tmp").exists())
        for corruption in ({"created_by": "auto-p2.py", "vms": ["../../somewhere"]},
                           {**state, "vms": ["c1", "fw", "lb", "../../etc"]},
                           {**state, "host_routes_added": ["192.168.0.0/16"]},
                           {**state, "bridges_added": ["RED1", "RED1"]},
                           [], None):
            with self.subTest(corruption=repr(corruption)):
                self.app.STATE.write_text(json.dumps(corruption))
                with self.assertRaises(self.lib.ErrorEscenario):
                    self.app.load_state()

    def test_stop_y_undefine_permitidos_con_json_roto(self):
        self.app.CONFIG.write_text("{invalid JSON")
        for operation in ("stop", "undefine"):
            with self.subTest(operation=operation):
                with patch.object(self.app, operation) as mock_op:
                    with patch.object(sys, "argv", ["auto-p2.py", operation]):
                        self.assertEqual(self.app.main(), 0)
                    mock_op.assert_called_once_with()
        with patch.object(self.app, "start") as mock_start:
            with patch.object(sys, "argv", ["auto-p2.py", "start"]):
                self.assertEqual(self.app.main(), 1)
            mock_start.assert_not_called()

    def test_generacion_xml_cloudinit_y_backing_file(self):
        calls = []
        def mock_command(args, **kwargs):
            args = [str(arg) for arg in args]
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(self.lib, "ejecutar", side_effect=mock_command):
            for name, interfaces, routing in self.app.specifications(5):
                with self.subTest(name=name):
                    vm = self.lib.VM(name)
                    vm.define_vm(self.work / "cdps-vm-base-pc1.qcow2", interfaces, forwarding=routing)
                    xml = ET.parse(vm.xml)
                    self.assertEqual(xml.findtext("name"), name)
                    self.assertEqual(xml.find("./devices/disk[@device='disk']/source").get("file"), str(vm.disk))
                    self.assertEqual(xml.find("./devices/disk[@device='cdrom']/source").get("file"), str(vm.iso))
                    self.assertEqual([x.find("source").get("bridge") for x in xml.findall("./devices/interface")],
                                     [x["bridge"] for x in interfaces])
                    self.assertIn("local-hostname: " + name, (vm.cloud_dir / "meta-data").read_text())
                    network_config = (vm.cloud_dir / "network-config").read_text()
                    for iface in interfaces:
                        self.assertIn(iface["addr"], network_config)
                        for route in iface.get("routes", []):
                            self.assertIn(route["via"], network_config)
                    user_data = (vm.cloud_dir / "user-data").read_text()
                    self.assertIn("hostname: " + name, user_data)
                    self.assertEqual("99-cdps-ipforward.conf" in user_data, routing)
        self.assertEqual(len([x for x in calls if x[:2] == ["qemu-img", "create"]]), 8)
        self.assertEqual(len([x for x in calls if x[0] == "genisoimage"]), 8)
        self.assertEqual(len([x for x in calls if x[:3] == ["sudo", "virsh", "define"]]), 8)

    def test_xml_invalido_activa_rollback(self):
        self.app.TEMPLATE.write_text("<domain><devices>")

        def fake_command(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(self.lib, "ejecutar", side_effect=fake_command):
            with patch.object(self.app, "host_network"):
                with patch.object(self.app, "undefine") as rollback:
                    with self.assertRaisesRegex(
                        self.lib.ErrorEscenario, "Plantilla XML inválida"
                    ):
                        self.app.define(1)

                    rollback.assert_called_once_with()

    def test_define_si_falla_invoca_rollback(self):
        app = self.app
        # Ninguna VM existe ni hay ficheros generados: corresponde al preflight normal.
        class FakeVM:
            def __init__(self, name):
                self.name = name
                self.xml = app.ROOT / (name + ".xml")
                self.disk = app.ROOT / (name + ".qcow2")
                self.iso = app.ROOT / (name + "-cdata.iso")
            def is_defined(self):
                return False
            def define_vm(self, *args, **kwargs):
                if self.name == "fw":
                    raise self_error("fallo simulado de virsh")
        self_error = self.lib.ErrorEscenario
        class FakeNET:
            def __init__(self, name):
                self.name = name
            def exists(self):
                return False
            def create_net(self):
                return True
        with patch.object(app, "VM", FakeVM), patch.object(app, "NET", FakeNET), \
             patch.object(app, "host_network"), patch.object(app, "undefine") as rollback:
            with self.assertRaisesRegex(self.lib.ErrorEscenario, "fallo simulado"):
                app.define(1)
            rollback.assert_called_once_with()

    def test_ovs_distingue_existencia_y_errores(self):
        net = self.lib.NET("RED1")
        cmd = ["sudo", "ovs-vsctl", "br-exists", "RED1"]

        for code, expected in ((0, True), (2, False)):
            with self.subTest(code=code):
                result = subprocess.CompletedProcess(cmd, code, "", "")
                with patch.object(self.lib, "ejecutar", return_value=result):
                    self.assertEqual(net.exists(), expected)

        failure = subprocess.CompletedProcess(
            cmd, 1, "", "Fallo simulado de OVS"
        )
        with patch.object(self.lib, "ejecutar", return_value=failure):
            with self.assertRaisesRegex(
                self.lib.ErrorEscenario, "Fallo simulado de OVS"
            ):
                net.exists()

    def test_ovs_error_en_undefine_conserva_estado(self):
        app = self.app
        state = {
            "created_by": "auto-p2.py",
            "vms": ["c1", "fw", "lb", "s1"],
            "bridges_added": ["RED1"],
            "host_address_added": False,
            "host_routes_added": [],
        }
        app.save_state(state)

        class FakeVM:
            def __init__(self, name):
                self.name = name

            def stop_vm(self):
                pass

            def state(self):
                return "shut off"

            def undefine_vm(self):
                pass

            def remove_files(self):
                pass

        failure = subprocess.CompletedProcess(
            ["sudo", "ovs-vsctl", "br-exists", "RED1"],
            1, "", "Fallo simulado de OVS"
        )

        with patch.object(app, "VM", FakeVM), patch.object(
            self.lib, "ejecutar", return_value=failure
        ):
            with self.assertRaisesRegex(
                self.lib.ErrorEscenario, "Fallo simulado de OVS"
            ):
                app.undefine()

        self.assertTrue(app.STATE.exists())
        self.assertEqual(app.load_state(), state)

    def test_virsh_distingue_ausencia_y_errores(self):
        vm = self.lib.VM("s1")

        def maquina_existente(args, **kwargs):
            if args[2] == "list":
                return subprocess.CompletedProcess(
                    args, 0, "\n".join(("c1", "fw", "lb", "s1", "")), ""
                )
            if args[2] == "domstate":
                return subprocess.CompletedProcess(
                    args, 1, "", "Fallo de conexión simulado"
                )
            self.fail(f"Comando inesperado: {args}")

        with patch.object(
            self.lib, "ejecutar", side_effect=maquina_existente
        ) as comandos:
            self.assertTrue(vm.is_defined())

            with self.assertRaisesRegex(
                self.lib.ErrorEscenario, "No se pudo consultar"
            ):
                vm.state()

            with self.assertRaises(self.lib.ErrorEscenario):
                vm.undefine_vm()

            self.assertFalse(any(
                call.args[0][2] == "undefine"
                for call in comandos.call_args_list
            ))

        def maquina_ausente(args, **kwargs):
            if args[2] == "list":
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[2] == "domstate":
                return subprocess.CompletedProcess(
                    args, 1, "", "Dominio inexistente"
                )
            self.fail(f"Comando inesperado: {args}")

        with patch.object(
            self.lib, "ejecutar", side_effect=maquina_ausente
        ):
            self.assertFalse(vm.is_defined())
            self.assertIsNone(vm.state())

        with patch.object(
            self.lib, "ejecutar",
            side_effect=self.lib.ErrorEscenario("libvirt no disponible")
        ):
            with self.assertRaisesRegex(
                self.lib.ErrorEscenario, "libvirt no disponible"
            ):
                vm.is_defined()

    def test_cli_rechaza_orden_desconocida(self):
        for argv in ([], ["invalid"], ["define", "extra"]):
            with self.subTest(argv=argv):
                process = subprocess.run(
                    [sys.executable, str(self.work / "auto-p2.py"), *argv],
                    text=True, capture_output=True, cwd=self.work, timeout=5,
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("usage:", process.stderr)

    def test_logging_y_debug_configurables(self):
        self.app.CONFIG.write_text('{"num_servers": 1}')
        self.assertFalse(self.app.read_config().get("debug", False))
        self.app.CONFIG.write_text('{"num_servers": 1, "debug": true}')
        self.assertTrue(self.app.read_config()["debug"])
        for bad in ("yes", 1, [], None):
            with self.subTest(value=bad):
                self.app.CONFIG.write_text(json.dumps({"num_servers": 1, "debug": bad}))
                with self.assertRaises(self.lib.ErrorEscenario):
                    self.app.read_config()

    def test_undefine_elimina_solo_inventario_simulado(self):
        app = self.app
        state = self.valid_state(2)
        app.save_state(state)
        destroyed_vms, removed_files, removed_bridges, cleaned_routes = [], [], [], []

        class FakeVM:
            def __init__(self, name):
                self.name = name
            def state(self):
                return "shut off"
            def stop_vm(self):
                pass
            def undefine_vm(self):
                destroyed_vms.append(self.name)
            def remove_files(self):
                removed_files.append(self.name)

        class FakeNET:
            def __init__(self, name):
                self.name = name
            def destroy_net(self):
                removed_bridges.append(self.name)

        def fake_exec(args, **kwargs):
            cleaned_routes.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(app, "VM", FakeVM), patch.object(app, "NET", FakeNET), \
             patch.object(app, "ejecutar", side_effect=fake_exec):
            app.undefine()

        self.assertEqual(destroyed_vms, list(reversed(state["vms"])))
        self.assertEqual(removed_files, state["vms"])
        self.assertEqual(removed_bridges, list(reversed(state["bridges_added"])))
        self.assertFalse(app.STATE.exists())
        self.assertEqual(len(cleaned_routes), 3)  # 2 rutas, 1 dirección
        self.assertEqual(sum("route" in args for args in cleaned_routes), 2)

    def test_rollback_recupera_recursos_parciales_simulados(self):
        app = self.app
        known_vms, bridges = set(), set()

        class FakeVM:
            def __init__(self, name):
                self.name = name
                self.disk = app.ROOT / f"{name}.qcow2"
                self.xml = app.ROOT / f"{name}.xml"
                self.iso = app.ROOT / f"{name}-cdata.iso"
            def is_defined(self):
                return self.name in known_vms
            def state(self):
                return "shut off" if self.name in known_vms else None
            def stop_vm(self):
                pass
            def define_vm(self, *args, **kwargs):
                if self.name == "fw":
                    self.disk.write_text("parcial")
                    raise self_error("fallo simulado en fw")
                known_vms.add(self.name)
                self.disk.write_text("contenido de prueba")
                self.xml.write_text("xml de prueba")
            def undefine_vm(self):
                known_vms.discard(self.name)
            def remove_files(self):
                for path in (self.disk, self.xml, self.iso):
                    path.unlink(missing_ok=True)

        self_error = self.lib.ErrorEscenario
        class FakeNET:
            def __init__(self, name):
                self.name = name
            def exists(self):
                return self.name in bridges
            def create_net(self):
                if self.name in bridges:
                    return False
                bridges.add(self.name)
                return True
            def destroy_net(self):
                bridges.discard(self.name)

        with patch.object(app, "VM", FakeVM), patch.object(app, "NET", FakeNET), \
             patch.object(app, "host_network"):
            with self.assertRaisesRegex(self_error, "fallo simulado en fw"):
                app.define(1)

        self.assertFalse(known_vms)
        self.assertFalse(bridges)
        self.assertFalse(app.STATE.exists())
        self.assertEqual({x.name for x in self.work.glob("*.qcow2")}, {"cdps-vm-base-pc1.qcow2"})

    def test_define_si_no_puede_revertir_preserva_estado(self):
        app = self.app
        class FakeVM:
            def __init__(self, name):
                self.xml = app.ROOT / (name + ".xml")
                self.disk = app.ROOT / (name + ".qcow2")
                self.iso = app.ROOT / (name + "-cdata.iso")
            def is_defined(self):
                return False
        with patch.object(app, "VM", FakeVM), patch.object(app, "NET") as fake_net, \
             patch.object(app, "undefine", side_effect=self.lib.ErrorEscenario("limpieza falló")):
            fake_net.return_value.exists.return_value = False
            fake_net.return_value.create_net.side_effect = self.lib.ErrorEscenario("fallo de bridge")
            with self.assertRaisesRegex(self.lib.ErrorEscenario, "Conservamos el estado"):
                app.define(1)
        self.assertTrue(app.STATE.exists())
        self.assertEqual(app.load_state()["vms"], ["c1", "fw", "lb", "s1"])


    def test_fallo_guardado_estado_no_deja_bridge_huerfano(self):
        app = self.app
        bridges = set()
        original_save = app.save_state
        saves = 0

        class FakeVM:
            def __init__(self, name):
                self.name = name
                self.xml = app.ROOT / f"{name}.xml"
                self.disk = app.ROOT / f"{name}.qcow2"
                self.iso = app.ROOT / f"{name}-cdata.iso"

            def is_defined(self):
                return False

            def stop_vm(self):
                pass

            def state(self):
                return None

            def undefine_vm(self):
                pass

            def remove_files(self):
                pass

        class FakeNET:
            def __init__(self, name):
                self.name = name

            def exists(self):
                return self.name in bridges

            def create_net(self):
                if self.exists():
                    return False
                bridges.add(self.name)
                return True

            def destroy_net(self):
                bridges.discard(self.name)

        def save_with_failure(state):
            nonlocal saves
            saves += 1
            if saves == 2:
                raise self.lib.ErrorEscenario("fallo simulado de guardado")
            original_save(state)

        with patch.object(app, "VM", FakeVM), patch.object(app, "NET", FakeNET), \
             patch.object(app, "save_state", side_effect=save_with_failure), \
             patch.object(app, "host_network"):
            with self.assertRaisesRegex(
                self.lib.ErrorEscenario, "fallo simulado de guardado"
            ):
                app.define(1)

        self.assertFalse(bridges, "No deben quedar bridges sin registrar")
        self.assertFalse(app.STATE.exists(), "El rollback debe limpiar el estado")

    def test_fallo_guardado_no_añade_ip_ni_ruta_sin_inventario(self):
        app = self.app
        for mode in ("direccion", "ruta"):
            with self.subTest(mode=mode):
                calls = []
                state = self.valid_state()
                state["host_address_added"] = False
                state["host_routes_added"] = []

                def fake_exec(args, **kwargs):
                    calls.append(args)
                    output = ""
                    if args[:4] == ["ip", "-4", "-o", "addr"] and mode == "ruta":
                        output = "4: RED1 inet 10.0.10.3/26 scope global RED1"
                    return subprocess.CompletedProcess(args, 0, output, "")

                with patch.object(app, "ejecutar", side_effect=fake_exec), \
                     patch.object(
                         app, "save_state",
                         side_effect=self.lib.ErrorEscenario("fallo guardado")
                     ):
                    with self.assertRaisesRegex(
                        self.lib.ErrorEscenario, "fallo guardado"
                    ):
                        app.host_network(state)

                operation = "addr" if mode == "direccion" else "route"
                self.assertFalse(
                    any(args[:4] == ["sudo", "ip", operation, "add"] for args in calls),
                    "No se debe crear un recurso sin haber registrado su propiedad",
                )

    def test_start_no_crea_bridge_sin_guardar_su_inventario(self):
        app = self.app
        state = self.valid_state()
        state["bridges_added"] = []
        app.save_state(state)
        bridges = set()

        class FakeNET:
            def __init__(self, name):
                self.name = name

            def exists(self):
                return self.name in bridges

            def create_net(self):
                bridges.add(self.name)
                return True

        with patch.object(app, "NET", FakeNET), patch.object(
            app, "save_state",
            side_effect=self.lib.ErrorEscenario("fallo de guardado en start")
        ):
            with self.assertRaisesRegex(
                self.lib.ErrorEscenario, "fallo de guardado en start"
            ):
                app.start()

        self.assertFalse(bridges)
        self.assertTrue(app.STATE.exists())

    def test_start_parcial_se_notifica_y_sigue_siendo_recuperable(self):
        app = self.app
        app.save_state(self.valid_state())
        events = []

        class FakeNET:
            def __init__(self, name):
                pass

            def exists(self):
                return True

            def create_net(self):
                return False

        class FakeVM:
            def __init__(self, name):
                self.name = name

            def start_vm(self):
                events.append(self.name)
                if self.name == "lb":
                    raise self_error("arranque fallido simulado")

            def stop_vm(self):
                pass

            def state(self):
                return "shut off"

            def show_console_vm(self):
                raise AssertionError("No debe abrir consolas tras fallar el arranque")

        self_error = self.lib.ErrorEscenario
        with patch.object(app, "NET", FakeNET), patch.object(app, "VM", FakeVM), \
             patch.object(app, "host_network"):
            with self.assertRaisesRegex(self_error, "arranque fallido simulado"):
                app.start()
            self.assertEqual(events, ["fw", "lb"])
            self.assertTrue(app.STATE.exists())
            app.stop()  # La operación de recuperación sigue disponible.

    def test_cli_aplica_realmente_nivel_info_y_debug(self):
        import logging

        for debug, level in ((False, logging.INFO), (True, logging.DEBUG)):
            with self.subTest(debug=debug):
                self.app.CONFIG.write_text(
                    json.dumps({"num_servers": 1, "debug": debug})
                )
                with patch.object(self.app, "define") as define, \
                     patch.object(sys, "argv", ["auto-p2.py", "define"]), \
                     patch.object(logging, "basicConfig") as configure:
                    self.assertEqual(self.app.main(), 0)
                define.assert_called_once_with(1)
                self.assertEqual(configure.call_args.kwargs["level"], level)
                self.assertIs(configure.call_args.kwargs["stream"], sys.stdout)



if __name__ == "__main__":
    unittest.main(verbosity=2)
