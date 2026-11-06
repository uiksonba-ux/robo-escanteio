import threading
import unittest

# robo.py starts its worker when imported. Keep this test isolated from all
# network activity and trading threads.
_original_start = threading.Thread.start
threading.Thread.start = lambda self: None
try:
    import robo
finally:
    threading.Thread.start = _original_start


class IndeterminateOrderIsolationTests(unittest.TestCase):
    def setUp(self):
        with robo.estado_lock:
            robo.envios_indeterminados.clear()
            robo.ativos_em_uso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    ocupada=False,
                    ativo=None,
                    ciclo_em_execucao=False,
                    envio_indeterminado=False,
                )

            # A sent order with unknown confirmation must retain both its bank
            # and asset reservation while reconciliation continues.
            banca = robo.BANCAS[0]
            ativo = "WLDAUD"
            robo.estado_frentes[banca].update(ocupada=True, ativo=ativo)
            robo.ativos_em_uso.add(ativo)
            robo.envios_indeterminados[banca] = {
                "banca": banca,
                "ativo": ativo,
                "nivel": 0,
                "valor": 2.0,
                "motivo": "timeout",
            }
            robo.estado_frentes[banca]["envio_indeterminado"] = True

    def test_other_nine_banks_can_take_distinct_signals(self):
        reserved = []
        for index in range(9):
            banca = robo.reservar_banca(
                f"TEST{index}", "call", 80, 1, 1
            )
            self.assertIsNotNone(banca)
            reserved.append(banca)

        self.assertEqual(reserved, robo.BANCAS[1:])
        self.assertEqual(len(set(reserved)), 9)
        self.assertEqual(robo.estado_frentes[robo.BANCAS[0]]["ativo"], "WLDAUD")
        self.assertIn("WLDAUD", robo.ativos_em_uso)

    def test_unknown_order_asset_cannot_be_reused(self):
        self.assertIsNone(
            robo.reservar_banca("WLDAUD", "put", 90, 1, 1)
        )
        self.assertIn("WLDAUD", robo.ativos_em_uso)


if __name__ == "__main__":
    unittest.main()
