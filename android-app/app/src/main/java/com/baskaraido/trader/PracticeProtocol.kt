package com.baskaraido.trader

import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.net.URL

/** Wire validation is shared by polling and configuration writes; no trading API exists here. */
object PracticeProtocol {
    private const val HOST = "baskaraido-monitor-test.onrender.com"
    val starts = intArrayOf(0, 50, 60, 70, 80)
    val ends = intArrayOf(49, 59, 69, 79, 100)

    fun address(base: String, path: String): URL {
        require(path == "/api/v1/monitor" || path == "/api/v1/practice/faixas")
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

    data class Bank(val wins: Int, val losses: Int, val balance: Double?)
    data class Snapshot(val wins: Int, val losses: Int, val accuracy: Double,
                        val banks: List<Bank>, val robotData: Boolean, val stale: Boolean)
    data class Band(val gales: Int, val multiplier: Double)
    data class Config(val enabled: Boolean, val bands: List<Band>, val revision: Int,
                      val appliedRevision: Int?, val configId: String, val appliedConfigId: String?)

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
            Bank(count(row.get("wins")), count(row.get("losses")), balance)
        }
        require(banks.sumOf { it.wins.toLong() } == wins.toLong())
        require(banks.sumOf { it.losses.toLong() } == losses.toLong())
        require(!json.has("stale") || json.get("stale") is Boolean)
        return Snapshot(wins, losses, accuracy, banks, origin == "robo_practice_memoria_volatil",
            json.optBoolean("stale", false))
    }

    fun config(json: JSONObject): Config {
        checkedRoot(json)
        require(json.get("enabled") is Boolean && json.getString("persistencia") == "memoria_temporaria")
        val revision = count(json.get("revision"))
        require(revision >= 1)
        val applied = if (json.isNull("aplicada_revision")) null else count(json.get("aplicada_revision"))
        require(applied == null || applied >= 1)
        val rows = json.getJSONArray("faixas")
        require(rows.length() == 5)
        val bands = (0..4).map { i ->
            val row = rows.getJSONObject(i)
            require(row.get("min") == starts[i] && row.get("max") == ends[i])
            require(row.get("mult") is Number)
            val gales = count(row.get("gales")); val mult = row.getDouble("mult")
            require(gales in 0..9 && mult.isFinite() && mult in 1.0..10.0)
            Band(gales, mult)
        }
        val configId = json.optString("config_id", "")
        require(configId.matches(Regex("[0-9a-f]{64}")))
        val appliedId = if (json.isNull("aplicada_config_id")) null else json.getString("aplicada_config_id")
        require(appliedId == null || appliedId.matches(Regex("[0-9a-f]{64}")))
        return Config(json.getBoolean("enabled"), bands, revision, applied, configId, appliedId)
    }

    fun payload(enabled: Boolean, bands: List<Band>, expectedRevision: Int, configId: String): JSONObject {
        require(bands.size == 5 && expectedRevision >= 1 && configId.matches(Regex("[0-9a-f]{64}")))
        val rows = JSONArray()
        bands.forEachIndexed { i, band ->
            require(band.gales in 0..9 && band.multiplier.isFinite() && band.multiplier in 1.0..10.0)
            rows.put(JSONObject().put("min", starts[i]).put("max", ends[i])
                .put("gales", band.gales).put("mult", band.multiplier))
        }
        return JSONObject().put("schema_version", 1).put("conta", "PRACTICE")
            .put("enabled", enabled).put("faixas", rows).put("expected_revision", expectedRevision)
            .put("expected_config_id", configId)
    }
}
