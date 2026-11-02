package com.baskaraido.trader

import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.net.URL
import java.time.Instant
import java.util.UUID

/** Wire validation is shared by polling and configuration writes; no trading API exists here. */
object PracticeProtocol {
    private const val HOST = "baskaraido-monitor-test.onrender.com"
    val starts = intArrayOf(0, 50, 60, 70, 80)
    val ends = intArrayOf(49, 59, 69, 79, 100)

    fun address(base: String, path: String): URL {
        require(path in setOf("/api/v1/monitor", "/api/v1/practice/faixas",
            "/api/v1/practice/settings", "/api/v1/practice/control"))
        val uri = URI(base.trim())
        require(uri.scheme == "https" && uri.host == HOST && uri.rawAuthority == HOST)
        require(uri.rawUserInfo == null && uri.port == -1 && uri.rawQuery == null && uri.rawFragment == null)
        require(uri.rawPath.isNullOrEmpty() || uri.rawPath == "/")
        return URL("https://$HOST$path")
    }

    fun token(value: String): String {
        val clean = value.trim()
        require(clean.isNotEmpty() && clean.length <= 4096 && clean.all { it.code in 33..126 })
        return clean
    }

    private fun count(value: Any): Int {
        require(value is Number && value.toDouble().isFinite())
        val n = value.toDouble()
        require(n >= 0 && n <= Int.MAX_VALUE && n == n.toInt().toDouble())
        return n.toInt()
    }

    private fun checkedRoot(json: JSONObject) {
        require(json.get("schema_version") == 1 && json.getString("conta") == "PRACTICE")
    }

    private fun money(json: JSONObject, key: String): Double? {
        if (!json.has(key) || json.isNull(key)) return null
        require(json.get(key) is Number)
        return json.getDouble(key).also { require(it.isFinite()) }
    }

    private fun optionalBoolean(json: JSONObject, key: String): Boolean? {
        if (!json.has(key) || json.isNull(key)) return null
        require(json.get(key) is Boolean)
        return json.getBoolean(key)
    }

    data class Bank(val wins: Int, val losses: Int, val balance: Double?,
                    val asset: String? = null, val gales: Int? = null,
                    val multiplier: Double? = null, val recovering: Boolean? = null)
    data class Recovery(val loss: Double?, val planned: Double?, val stake: Double?,
                        val state: String, val bank: String?, val gales: Int?, val multiplier: Double?)
    data class Snapshot(val wins: Int, val losses: Int, val accuracy: Double,
                        val banks: List<Bank>, val robotData: Boolean, val stale: Boolean,
                        val iqConnected: Boolean?, val practiceBalance: Double?,
                        val assets: List<String>, val operations: List<String>, val recovery: Recovery?)
    data class Band(val gales: Int, val multiplier: Double, val min: Int? = null, val max: Int? = null)
    data class Config(val enabled: Boolean, val bands: List<Band>, val revision: Int,
                      val appliedRevision: Int?, val configId: String, val appliedConfigId: String?,
                      val effectiveBands: List<Band>? = null, val pending: Boolean = true,
                      val effectiveEnabled: Boolean? = null)
    data class Control(val revision: Int, val desiredPaused: Boolean, val confirmed: Boolean,
                       val pending: Boolean, val online: Boolean, val authorized: Boolean,
                       val paused: Boolean?, val appliedRevision: Int?, val error: String?) {
        val canResume: Boolean get() = online && authorized && confirmed && !pending && paused == true
        val canPause: Boolean get() = !desiredPaused
    }

    /** In-flight and failed requests stay unconfirmed until a fresh validated GET arrives. */
    class Confirmation {
        var control: Control? = null
            private set
        var uncertain: Boolean = true
            private set
        fun begin() { uncertain = true; control = null }
        fun failed() { begin() }
        fun observed(value: Control) { control = value; uncertain = false }
        val canResume: Boolean get() = !uncertain && control?.canResume == true
        val canPause: Boolean get() = !uncertain && control?.canPause == true
    }

    fun monitor(json: JSONObject): Snapshot {
        checkedRoot(json)
        require(json.getString("modo") == "somente_leitura")
        val origin = json.getString("origem")
        require(origin in setOf("ambiente_de_teste_sem_dados_reais", "robo_practice_memoria_volatil"))
        val stats = json.getJSONObject("estatisticas")
        val wins = count(stats.get("wins")); val losses = count(stats.get("losses"))
        val total = wins.toLong() + losses.toLong()
        val accuracy = if (total == 0L) 0.0 else wins * 100.0 / total
        require(stats.get("assertividade") is Number)
        val suppliedAccuracy = stats.getDouble("assertividade")
        require(suppliedAccuracy.isFinite() && kotlin.math.abs(suppliedAccuracy - accuracy) <= 0.011)
        val entries = json.getJSONObject("bancas")
        require(entries.length() == 10)
        val banks = (1..10).map { index ->
            val row = entries.getJSONObject("BANCA $index")
            val balance = if (row.has("saldo") && !row.isNull("saldo")) row.getDouble("saldo") else null
            require(balance == null || (row.get("saldo") is Number && balance.isFinite()))
            val gales = if (row.has("gales") && !row.isNull("gales")) count(row.get("gales")) else null
            require(gales == null || gales in 0..9)
            Bank(count(row.get("wins")), count(row.get("losses")), balance,
                if (row.has("ativo") && !row.isNull("ativo")) row.getString("ativo").take(80) else null,
                gales, money(row, "multiplicador"), optionalBoolean(row, "em_recuperacao"))
        }
        require(banks.sumOf { it.wins.toLong() } == wins.toLong())
        require(banks.sumOf { it.losses.toLong() } == losses.toLong())
        require(!json.has("stale") || json.get("stale") is Boolean)
        val assets = json.optJSONArray("ativos")?.let { rows ->
            require(rows.length() <= 100)
            (0 until rows.length()).map { rows.getString(it).take(80) }
        } ?: emptyList()
        val operations = json.optJSONArray("operacoes")?.let { rows ->
            require(rows.length() <= 100)
            (0 until rows.length()).map { index ->
                val row = rows.getJSONObject(index)
                val bank = row.optString("banca", "Banca").take(40)
                val asset = row.optString("ativo", "—").take(80)
                val stage = if (row.has("nivel") && !row.isNull("nivel")) count(row.get("nivel")).toString() else "—"
                val stake = (money(row, "valor") ?: money(row, "entrada"))?.let {
                    String.format(java.util.Locale.getDefault(), "%.2f", it)
                } ?: "—"
                "$bank · $asset · nível $stage · entrada $stake"
            }
        } ?: emptyList()
        val recovery = json.optJSONObject("gestao")?.let { row ->
            val gales = if (row.has("gales_proximos") && !row.isNull("gales_proximos")) count(row.get("gales_proximos")) else null
            require(gales == null || gales in 0..9)
            Recovery(money(row, "prejuizo_acumulado"), money(row, "recuperacao_planejada"),
                money(row, "entrada_recuperacao_planejada"), row.optString("recuperacao_estado", "desconhecido"),
                if (row.has("recuperacao_em_curso") && !row.isNull("recuperacao_em_curso")) row.getString("recuperacao_em_curso").take(80) else null,
                gales, money(row, "multiplicador_proximo"))
        }
        return Snapshot(wins, losses, accuracy, banks, origin == "robo_practice_memoria_volatil",
            json.optBoolean("stale", false), optionalBoolean(json, "iq_conectada"),
            money(json, "saldo_practice"), assets, operations, recovery)
    }

    fun config(json: JSONObject): Config {
        checkedRoot(json)
        require(json.get("enabled") is Boolean && json.getString("persistencia") in setOf("memoria_temporaria", "sqlite"))
        val revision = count(json.get("revision"))
        require(revision >= 1)
        val applied = if (json.isNull("aplicada_revision")) null else count(json.get("aplicada_revision"))
        require(applied == null || applied >= 1)
        val parsedBands = bands(json.getJSONArray("faixas"))
        val configId = json.optString("config_id", "")
        require(configId.matches(Regex("[0-9a-f]{64}")))
        val appliedId = if (json.isNull("aplicada_config_id")) null else json.getString("aplicada_config_id")
        require(appliedId == null || appliedId.matches(Regex("[0-9a-f]{64}")))
        val effective = json.optJSONObject("effective_config")?.let {
            checkedRoot(it)
            require(it.get("enabled") is Boolean)
            bands(it.getJSONArray("faixas"))
        }
        val pending = optionalBoolean(json, "pending") ?: (applied != revision || appliedId != configId)
        require(pending || (applied == revision && appliedId == configId))
        return Config(json.getBoolean("enabled"), parsedBands, revision, applied, configId, appliedId, effective, pending,
            json.optJSONObject("effective_config")?.getBoolean("enabled"))
    }

    private fun bands(rows: JSONArray): List<Band> {
        require(rows.length() == 5)
        return validatedBands((0..4).map { i ->
            val row = rows.getJSONObject(i)
            require(row.get("min") is Int && row.get("max") is Int)
            require(row.get("mult") is Number)
            val gales = count(row.get("gales")); val mult = row.getDouble("mult")
            Band(gales, mult, row.getInt("min"), row.getInt("max"))
        })
    }

    fun validatedBands(values: List<Band>): List<Band> {
        require(values.size == 5)
        var expectedMin = 0
        val normalized = values.mapIndexed { i, band ->
            require((band.min == null) == (band.max == null))
            val lower = band.min ?: starts[i]
            val upper = band.max ?: ends[i]
            require(lower == expectedMin && lower in 0..100 && upper in lower..100)
            require(band.gales in 0..9 && band.multiplier.isFinite() && band.multiplier in 1.0..10.0)
            expectedMin = upper + 1
            band.copy(min = lower, max = upper)
        }
        require(expectedMin == 101)
        return normalized
    }

    fun bandIndex(accuracy: Double, values: List<Band>): Int? {
        if (!accuracy.isFinite() || accuracy !in 0.0..100.0) return null
        val normalized = validatedBands(values)
        return normalized.indexOfFirst { accuracy >= it.min!! && accuracy < it.max!! + 1.0 }.takeIf { it >= 0 }
    }

    fun control(json: JSONObject): Control {
        checkedRoot(json)
        val revision = count(json.get("revision")); require(revision >= 1)
        require(UUID.fromString(json.getString("command_id")).toString() == json.getString("command_id"))
        val desired = optionalBoolean(json, "desired_paused") ?: throw IllegalArgumentException()
        val confirmed = optionalBoolean(json, "confirmed") ?: throw IllegalArgumentException()
        val pending = optionalBoolean(json, "pending") ?: throw IllegalArgumentException()
        val online = optionalBoolean(json, "robot_online") ?: throw IllegalArgumentException()
        val authorized = optionalBoolean(json, "authorized") ?: throw IllegalArgumentException()
        val paused = optionalBoolean(json, "paused")
        val applied = if (json.isNull("applied_revision")) null else count(json.get("applied_revision"))
        require(applied == null || applied >= 1)
        require(!confirmed || (online && !pending && applied == revision && paused == desired))
        require(paused != false || authorized)
        require(confirmed || paused == null)
        return Control(revision, desired, confirmed, pending, online, authorized, paused, applied,
            if (json.isNull("error")) null else json.getString("error").take(200))
    }

    fun command(action: String, expectedRevision: Int): JSONObject {
        require(action in setOf("pause_new_entries", "resume_new_entries") && expectedRevision >= 1)
        return commandEnvelope().put("action", action).put("expected_revision", expectedRevision)
    }

    fun commandEnvelope(): JSONObject = JSONObject().put("schema_version", 1).put("conta", "PRACTICE")
        .put("command_id", UUID.randomUUID().toString()).put("expires_at", Instant.now().plusSeconds(90).toString())

    fun settingsPayload(enabled: Boolean, bands: List<Band>, revision: Int, configId: String): JSONObject {
        val result = payload(enabled, bands, revision, configId)
        val envelope = commandEnvelope()
        return result.put("command_id", envelope.getString("command_id")).put("expires_at", envelope.getString("expires_at"))
    }

    fun payload(enabled: Boolean, bands: List<Band>, expectedRevision: Int, configId: String): JSONObject {
        require(expectedRevision >= 1 && configId.matches(Regex("[0-9a-f]{64}")))
        val rows = JSONArray()
        validatedBands(bands).forEach { band ->
            rows.put(JSONObject().put("min", band.min).put("max", band.max)
                .put("gales", band.gales).put("mult", band.multiplier))
        }
        return JSONObject().put("schema_version", 1).put("conta", "PRACTICE")
            .put("enabled", enabled).put("faixas", rows).put("expected_revision", expectedRevision)
            .put("expected_config_id", configId)
    }
}

