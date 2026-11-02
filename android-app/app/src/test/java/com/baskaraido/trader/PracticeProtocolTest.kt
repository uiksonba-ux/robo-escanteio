package com.baskaraido.trader

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.util.UUID

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

    @Test fun `config cannot cross real account or use overlapping bounds`() {
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

    private fun control(confirmed: Boolean = true, authorized: Boolean = true): JSONObject = JSONObject()
        .put("schema_version", 1).put("conta", "PRACTICE").put("revision", 4)
        .put("command_id", UUID.randomUUID().toString()).put("desired_paused", true)
        .put("confirmed", confirmed).put("pending", !confirmed).put("robot_online", true)
        .put("authorized", authorized).put("paused", if (confirmed) true else JSONObject.NULL)
        .put("applied_revision", if (confirmed) 4 else JSONObject.NULL).put("error", JSONObject.NULL)

    @Test fun `only a fresh confirmed authorized paused robot permits resume`() {
        assertTrue(PracticeProtocol.control(control()).canResume)
        assertFalse(PracticeProtocol.control(control(authorized = false)).canResume)
        assertFalse(PracticeProtocol.control(control(confirmed = false)).canResume)
        assertFalse(PracticeProtocol.control(control(false).put("robot_online", false)).canPause)
        reject { PracticeProtocol.control(control().put("robot_online", false)) }
        reject { PracticeProtocol.control(control().put("applied_revision", 3)) }
        reject { PracticeProtocol.control(control().put("paused", false)) }
        reject { PracticeProtocol.control(control(false).put("paused", true)) }
        reject { PracticeProtocol.control(control().put("conta", "REAL")) }
        reject { PracticeProtocol.control(control().put("command_id", "invalid")) }
        val queuedResume = control(false).put("desired_paused", false)
        assertTrue(PracticeProtocol.control(queuedResume).canPause)
        assertTrue(PracticeProtocol.control(queuedResume.put("robot_online", false)).canPause)
        assertFalse(PracticeProtocol.control(queuedResume).canResume)
    }

    @Test fun `communication failure and queued commands invalidate earlier confirmation`() {
        val state = PracticeProtocol.Confirmation()
        state.observed(PracticeProtocol.control(control()))
        assertTrue(state.canResume)
        state.begin()
        assertFalse(state.canResume); assertNull(state.control)
        state.observed(PracticeProtocol.control(control(false)))
        assertFalse(state.canResume)
        state.observed(PracticeProtocol.control(control()))
        state.failed()
        assertFalse(state.canResume); assertFalse(state.canPause)
        state.observed(PracticeProtocol.control(control()))
        assertTrue(state.canResume)
    }

    @Test fun `commands carry unique replay protection revisions and a short expiry`() {
        val first = PracticeProtocol.command("pause_new_entries", 7)
        val second = PracticeProtocol.command("pause_new_entries", 7)
        assertEquals(7, first.getInt("expected_revision"))
        assertEquals("PRACTICE", first.getString("conta"))
        UUID.fromString(first.getString("command_id"))
        assertNotEquals(first.getString("command_id"), second.getString("command_id"))
        val lifetime = Instant.parse(first.getString("expires_at")).epochSecond - Instant.now().epochSecond
        assertTrue(lifetime in 1..90)
        reject { PracticeProtocol.command("start_robot", 1) }
        reject { PracticeProtocol.command("resume_new_entries", 0) }
        val settings = PracticeProtocol.settingsPayload(true, List(5) { PracticeProtocol.Band(1, 2.0) }, 9, configHash)
        assertEquals(9, settings.getInt("expected_revision"))
        assertEquals(configHash, settings.getString("expected_config_id"))
        UUID.fromString(settings.getString("command_id"))
        Instant.parse(settings.getString("expires_at"))
        assertEquals("$host/api/v1/practice/control", PracticeProtocol.address(host, "/api/v1/practice/control").toString())
    }

    @Test fun `durable settings expose effective rules separately from pending edits`() {
        val json = configuration().put("persistencia", "sqlite").put("pending", true)
        json.put("effective_config", configuration().put("enabled", false))
        val settings = PracticeProtocol.config(json)
        assertTrue(settings.pending)
        assertEquals(5, settings.effectiveBands!!.size)
        reject { PracticeProtocol.config(json.put("pending", false)) }
    }

    @Test fun `practice balance recovery and bank rules survive telemetry validation`() {
        val json = monitor().put("iq_conectada", true).put("saldo_practice", 1000.0)
            .put("ativos", JSONArray().put("EURUSD-OTC"))
            .put("operacoes", JSONArray().put(JSONObject().put("banca", "BANCA 1")
                .put("ativo", "EURUSD-OTC").put("nivel", 1).put("valor", 20.0)))
            .put("gestao", JSONObject().put("prejuizo_acumulado", 50.0)
                .put("recuperacao_planejada", 50.0).put("entrada_recuperacao_planejada", 72.5)
                .put("recuperacao_em_curso", "BANCA 1").put("recuperacao_estado", "em_andamento")
                .put("gales_proximos", 1).put("multiplicador_proximo", 2.0))
        json.getJSONObject("bancas").getJSONObject("BANCA 1").put("gales", 1)
            .put("multiplicador", 2.0).put("ativo", "EURUSD-OTC").put("em_recuperacao", true)
        val snapshot = PracticeProtocol.monitor(json)
        assertEquals(true, snapshot.iqConnected)
        assertEquals(1000.0, snapshot.practiceBalance!!, 0.0)
        assertEquals(50.0, snapshot.recovery!!.loss!!, 0.0)
        assertEquals("em_andamento", snapshot.recovery.state)
        assertEquals(1, snapshot.operations.size)
        assertEquals(1, snapshot.banks[0].gales)
        assertEquals(true, snapshot.banks[0].recovering)
        reject { PracticeProtocol.monitor(json.put("iq_conectada", "true")) }
    }

    @Test fun `custom interval limits are consecutive cover zero through hundred and round trip`() {
        val custom = listOf(0 to 39, 40 to 54, 55 to 74, 75 to 89, 90 to 100)
            .mapIndexed { i, (min, max) -> PracticeProtocol.Band(4 - i, 2.0, min, max) }
        val payload = PracticeProtocol.payload(true, custom, 3, configHash)
            .put("revision", 3).put("config_id", configHash).put("persistencia", "sqlite")
            .put("aplicada_revision", JSONObject.NULL).put("aplicada_config_id", JSONObject.NULL)
        assertEquals(custom, PracticeProtocol.config(payload).bands)
        assertEquals(0, PracticeProtocol.bandIndex(0.0, custom))
        assertEquals(0, PracticeProtocol.bandIndex(39.999, custom))
        assertEquals(1, PracticeProtocol.bandIndex(40.0, custom))
        assertEquals(2, PracticeProtocol.bandIndex(59.999, custom))
        assertEquals(4, PracticeProtocol.bandIndex(100.0, custom))
        reject { PracticeProtocol.validatedBands(custom.toMutableList().apply { this[0] = this[0].copy(min = 1) }) }
        reject { PracticeProtocol.validatedBands(custom.toMutableList().apply { this[1] = this[1].copy(min = 39) }) }
        reject { PracticeProtocol.validatedBands(custom.toMutableList().apply { this[1] = this[1].copy(min = 41) }) }
        reject { PracticeProtocol.validatedBands(custom.toMutableList().apply { this[4] = this[4].copy(max = 99) }) }
        reject { PracticeProtocol.validatedBands(custom.toMutableList().apply { this[4] = this[4].copy(max = 101) }) }
    }
}

