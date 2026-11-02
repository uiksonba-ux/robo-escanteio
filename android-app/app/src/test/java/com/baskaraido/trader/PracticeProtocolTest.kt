package com.baskaraido.trader

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PracticeProtocolTest {
    private val configHash = "a".repeat(64)
    private val host = "https://baskaraido-monitor-test.onrender.com"
    private fun reject(action: () -> Unit) {
        try { action(); fail("Unsafe wire value accepted") } catch (_: IllegalArgumentException) {}
    }
    private fun configuration(): JSONObject {
        val bands = (0..4).map { PracticeProtocol.Band(4 - it, 2.0) }
        return PracticeProtocol.payload(true, bands, 3, configHash)
            .put("revision", 3).put("config_id", configHash)
            .put("persistencia", "memoria_temporaria")
            .put("aplicada_revision", JSONObject.NULL).put("aplicada_config_id", JSONObject.NULL)
    }
    private fun monitor(): JSONObject {
        val banks = JSONObject()
        (1..10).forEach { i -> banks.put("BANCA $i", JSONObject()
            .put("wins", if (i == 1) 3 else 0).put("losses", if (i == 1) 1 else 0)
            .put("saldo", JSONObject.NULL)) }
        return JSONObject().put("schema_version", 1).put("conta", "PRACTICE")
            .put("modo", "somente_leitura").put("origem", "robo_practice_memoria_volatil")
            .put("estatisticas", JSONObject().put("wins", 3).put("losses", 1).put("assertividade", 75.0))
            .put("bancas", banks).put("stale", false)
    }

    @Test fun `only exact isolated host gets bearer requests`() {
        assertEquals("$host/api/v1/monitor", PracticeProtocol.address("$host/", "/api/v1/monitor").toString())
        listOf("http://baskaraido-monitor-test.onrender.com", "$host:443", "$host:8443",
            "https://user@baskaraido-monitor-test.onrender.com", "$host.attacker.example", "$host/path",
            "$host?next=evil", "$host#fragment", "https://baskaraido-monitor-test.onrender.com@evil.example",
            "https://baskaraido-monitor-test.onrender.com%2eevil.example").forEach { base ->
            reject { PracticeProtocol.address(base, "/api/v1/monitor") }
        }
        reject { PracticeProtocol.address(host, "/api/v1/trade") }
    }

    @Test fun `token rejects header injection and empty input`() {
        assertEquals("test-token", PracticeProtocol.token(" test-token "))
        listOf("", " \n", "token\r\nX-Evil: value", "tok en", "á").forEach { value ->
            reject { PracticeProtocol.token(value) }
        }
    }

    @Test fun `all band boundaries include zero and perfect accuracy`() {
        mapOf(0.0 to 0, 49.99 to 0, 50.0 to 1, 59.99 to 1, 60.0 to 2, 69.99 to 2,
            70.0 to 3, 79.99 to 3, 80.0 to 4, 100.0 to 4).forEach { (accuracy, index) ->
            assertEquals(index, AccuracyRanges.indexFor(accuracy))
        }
        listOf(-0.01, 100.01, Double.NaN, Double.POSITIVE_INFINITY).forEach {
            assertNull(AccuracyRanges.indexFor(it))
        }
    }

    @Test fun `config round trip carries revision and expected hash`() {
        val json = configuration()
        val config = PracticeProtocol.config(json)
        assertEquals(3, config.revision)
        assertNull(config.appliedRevision)
        assertEquals(listOf(4, 3, 2, 1, 0), config.bands.map { it.gales })
        val write = PracticeProtocol.payload(false, config.bands, config.revision, config.configId)
        assertEquals(3, write.getInt("expected_revision"))
        assertEquals(configHash, write.getString("expected_config_id"))
        assertFalse(write.getBoolean("enabled"))
        val oldProcess = PracticeProtocol.config(configuration().put("aplicada_revision", 10)
            .put("aplicada_config_id", "b".repeat(64)))
        assertNotEquals(oldProcess.configId, oldProcess.appliedConfigId)
    }

    @Test fun `config cannot cross real account or change fixed bounds`() {
        reject { PracticeProtocol.config(configuration().put("conta", "REAL")) }
        reject { PracticeProtocol.config(configuration().put("schema_version", 2)) }
        reject { PracticeProtocol.config(configuration().put("persistencia", "duravel")) }
        reject { PracticeProtocol.config(configuration().put("enabled", "true")) }
        reject { PracticeProtocol.config(configuration().put("aplicada_revision", 0)) }
        reject { PracticeProtocol.config(configuration().put("revision", 0)) }
        reject { PracticeProtocol.config(configuration().put("config_id", "unknown")) }
        val wrongBounds = configuration()
        wrongBounds.getJSONArray("faixas").getJSONObject(0).put("max", 50)
        reject { PracticeProtocol.config(wrongBounds) }
        reject { PracticeProtocol.config(configuration().put("faixas", JSONArray())) }
    }

    @Test fun `gales and multipliers must stay within safety ranges`() {
        listOf(PracticeProtocol.Band(-1, 2.0), PracticeProtocol.Band(10, 2.0),
            PracticeProtocol.Band(1, 0.9), PracticeProtocol.Band(1, 10.1),
            PracticeProtocol.Band(1, Double.NaN), PracticeProtocol.Band(1, Double.POSITIVE_INFINITY)).forEach { bad ->
            reject { PracticeProtocol.payload(true, List(5) { bad }, 1, configHash) }
        }
    }

    @Test fun `telemetry supplies all ten banks and calculated accuracy`() {
        val parsed = PracticeProtocol.monitor(monitor())
        assertEquals(3, parsed.wins); assertEquals(1, parsed.losses)
        assertEquals(75.0, parsed.accuracy, 0.00001)
        assertEquals(10, parsed.banks.size)
        assertTrue(parsed.robotData); assertFalse(parsed.stale)
        assertNull(parsed.banks[0].balance)
        assertTrue(PracticeProtocol.monitor(monitor().put("stale", true)).stale)
    }

    @Test fun `untrusted or inconsistent telemetry is rejected`() {
        reject { PracticeProtocol.monitor(monitor().put("conta", "REAL")) }
        reject { PracticeProtocol.monitor(monitor().put("modo", "execucao")) }
        reject { PracticeProtocol.monitor(monitor().put("origem", "unknown")) }
        val missing = monitor(); missing.getJSONObject("bancas").remove("BANCA 10")
        reject { PracticeProtocol.monitor(missing) }
        val inaccurate = monitor(); inaccurate.getJSONObject("estatisticas").put("assertividade", 99)
        reject { PracticeProtocol.monitor(inaccurate) }
        val negative = monitor(); negative.getJSONObject("estatisticas").put("wins", -1)
        reject { PracticeProtocol.monitor(negative) }
        val mismatch = monitor(); mismatch.getJSONObject("bancas").getJSONObject("BANCA 2").put("wins", 9)
        reject { PracticeProtocol.monitor(mismatch) }
    }
}
