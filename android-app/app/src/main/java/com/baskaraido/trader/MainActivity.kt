package com.baskaraido.trader

import android.app.Activity
import android.graphics.Color
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.Locale
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.*
import org.json.JSONObject
import java.net.HttpURLConnection

class MainActivity : Activity() {
    private val ink = Color.WHITE
    private val muted = Color.rgb(205, 215, 235)
    private val cardColor = Color.argb(226, 13, 23, 48)
    private lateinit var status: TextView
    private lateinit var banks: LinearLayout
    private lateinit var endpoint: EditText
    private lateinit var tokenField: EditText
    private lateinit var summaryStats: TextView
    private lateinit var accuracyStats: TextView
    private lateinit var dataOrigin: TextView
    private lateinit var rangeResult: TextView
    private var currentAccuracy: Double? = null
    private val handler = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private var poll: Runnable? = null
    private var activeTask: Future<*>? = null
    @Volatile private var connection: HttpURLConnection? = null
    private var generation = 0
    private var foreground = false
    private var session: Pair<String, String>? = null
    private var refreshAction: (() -> Unit)? = null

    override fun onStart() {
        super.onStart()
        foreground = true
        if (session != null) refreshAction?.invoke()
    }

    override fun onStop() {
        foreground = false
        generation++
        poll?.let(handler::removeCallbacks)
        activeTask?.cancel(true)
        connection?.disconnect()
        currentAccuracy = null
        super.onStop()
    }

    override fun onDestroy() {
        session = null
        worker.shutdownNow()
        super.onDestroy()
    }

    /** No redirects and bounded reads: the bearer token can only reach the isolated host. */
    private fun request(base: String, token: String, path: String, payload: JSONObject? = null): JSONObject {
        val conn = (PracticeProtocol.address(base, path).openConnection() as HttpURLConnection).apply {
            requestMethod = if (payload == null) "GET" else "PUT"
            connectTimeout = 10000; readTimeout = 10000
            instanceFollowRedirects = false
            setRequestProperty("Authorization", "Bearer ${PracticeProtocol.token(token)}")
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Cache-Control", "no-store")
            if (payload != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
            }
        }
        connection = conn
        try {
            payload?.let { body -> conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) } }
            if (conn.responseCode != 200) throw ApiFailure(conn.responseCode)
            val output = java.io.ByteArrayOutputStream()
            conn.inputStream.use { input ->
                val buffer = ByteArray(4096)
                while (true) {
                    val read = input.read(buffer)
                    if (read == -1) break
                    require(output.size() + read <= 65536)
                    output.write(buffer, 0, read)
                }
            }
            return JSONObject(String(output.toByteArray(), Charsets.UTF_8))
        } finally {
            connection = null
            conn.disconnect()
        }
    }

    private class ApiFailure(val code: Int) : Exception()

    private fun failureMessage(error: Exception): String = when {
        error is ApiFailure && error.code == 401 -> "Token rejeitado. Informe um token válido e reconecte."
        error is ApiFailure && error.code == 409 -> "Configuração mudou no servidor. Reconecte para revisar antes de enviar."
        error is ApiFailure -> "Servidor respondeu HTTP ${error.code}."
        else -> "Atualização indisponível. Dados exibidos podem estar desatualizados."
    }

    private fun dp(value: Int): Int = (resources.displayMetrics.density * value).toInt()
    private fun text(value: String, size: Float = 15f, bold: Boolean = false): TextView =
        TextView(this).apply {
            text = value; textSize = size; setTextColor(ink)
            if (bold) setTypeface(null, Typeface.BOLD)
            setPadding(0, dp(5), 0, dp(5))
        }
    private fun panel(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        background = GradientDrawable().apply {
            setColor(Color.argb(237, 10, 19, 43)); cornerRadius = dp(22).toFloat()
            setStroke(dp(1), Color.argb(105, 70, 195, 255))
        }
        setPadding(dp(16), dp(12), dp(16), dp(12))
    }
    private fun addPanel(parent: LinearLayout, view: View) {
        parent.addView(view, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(12) })
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val scroll = ScrollView(this).apply { isFillViewport = true }
        val wallpaperLayer = android.widget.FrameLayout(this)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(26), dp(18), dp(30))
            setBackgroundColor(Color.TRANSPARENT)
        }
        val wallpaper = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            try {
                setImageBitmap(assets.open("wallpaper.png").use { BitmapFactory.decodeStream(it) })
            } catch (_: Exception) {
                setImageDrawable(GradientDrawable(GradientDrawable.Orientation.TL_BR, intArrayOf(
                    Color.rgb(74, 11, 110), Color.rgb(7, 50, 108), Color.rgb(19, 13, 53)
                )))
            }
        }
        wallpaperLayer.addView(wallpaper, android.widget.FrameLayout.LayoutParams(-1, -1))
        wallpaperLayer.addView(View(this).apply {
            setBackgroundColor(Color.argb(45, 5, 9, 30))
        }, android.widget.FrameLayout.LayoutParams(-1, -1))
        scroll.addView(root)
        wallpaperLayer.addView(scroll, android.widget.FrameLayout.LayoutParams(-1, -1))
        setContentView(wallpaperLayer)

        root.addView(text("BASKARAIDO", 29f, true))
        root.addView(text("TRADER  PRO", 16f, true).apply { setTextColor(Color.rgb(56, 214, 255)) })
        val practice = text("● MONITORAMENTO  •  PRACTICE", 13f, true)
        practice.setTextColor(Color.rgb(105, 240, 195))
        root.addView(practice)
        root.addView(text("Painel de acompanhamento · sem envio de ordens", 12f))

        val summary = panel()
        summary.addView(text("VISÃO GERAL", 13f, true))
        status = text("Aguardando conexão autenticada com o Render.", 14f)
        summary.addView(status)
        dataOrigin = text("ORIGEM DOS DADOS: não conectado", 12f, true)
        summary.addView(dataOrigin)
        summaryStats = text("WIN     —        LOSS     —", 18f, true)
        summary.addView(summaryStats)
        accuracyStats = text("ASSERTIVIDADE     —", 16f, true).apply { setTextColor(Color.rgb(56, 214, 255)) }
        summary.addView(accuracyStats)
        addPanel(root, summary)

        val bankPanel = panel()
        bankPanel.addView(text("10 BANCAS", 18f, true))
        val bankHint = text("Valores indisponíveis até conectar a API segura.", 12f)
        bankPanel.addView(bankHint)
        banks = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        for (i in 1..10) {
            val item = text("●   BANCA %02d       SALDO —       W — / L —".format(i), 13f)
            item.setTextColor(if (i % 2 == 0) Color.rgb(81, 213, 255) else Color.rgb(226, 143, 255))
            banks.addView(item)
        }
        bankPanel.addView(banks)
        addPanel(root, bankPanel)

        val rules = panel()
        rules.addView(text("GESTÃO DE ASSERTIVIDADE", 17f, true))
        rules.addView(text("Alterações sincronizadas valem somente para novas operações", 12f))
        val strategyPrefs = getSharedPreferences("accuracy_ranges", MODE_PRIVATE)
        val toggle = Switch(this).apply {
            text = "Ativar gestão por faixas"
            setTextColor(ink)
            isChecked = strategyPrefs.getBoolean("enabled", true)
        }
        rules.addView(toggle)
        val limits = intArrayOf(49, 59, 69, 79, 100)
        val starts = intArrayOf(0, 50, 60, 70, 80)
        val defaults = intArrayOf(4, 3, 2, 1, 0)
        var dirtyRanges = false
        var loadingRanges = false
        var remoteConfig: PracticeProtocol.Config? = null
        val galeFields = mutableListOf<EditText>()
        val multFields = mutableListOf<EditText>()
        for (i in starts.indices) {
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
            row.addView(text("${starts[i]}–${limits[i]}%  ", 14f, true), LinearLayout.LayoutParams(0, -2, 1f))
            val gale = EditText(this).apply {
                setSingleLine(true); inputType = InputType.TYPE_CLASS_NUMBER
                setText(strategyPrefs.getInt("gale_$i", defaults[i]).toString())
                setTextColor(ink); hint = "Gales"
                contentDescription = "Quantidade de gales na faixa ${starts[i]} a ${limits[i]}%"
            }
            row.addView(gale, LinearLayout.LayoutParams(dp(60), -2))
            galeFields.add(gale)
            val mult = EditText(this).apply {
                setSingleLine(true); inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
                setText(strategyPrefs.getString("mult_$i", "2"))
                setTextColor(ink); hint = "×"
                contentDescription = "Multiplicador na faixa ${starts[i]} a ${limits[i]}%"
            }
            row.addView(mult, LinearLayout.LayoutParams(dp(65), -2))
            multFields.add(mult)
            rules.addView(row)
        }
        rangeResult = text("Faixa ainda não calculada.", 13f, true)
        rules.addView(rangeResult)
        fun updateRange() {
            val accuracy = currentAccuracy
            rangeResult.text = when {
                !toggle.isChecked -> "Gestão por faixas desativada."
                accuracy == null -> "Aguardando assertividade do monitor PRACTICE."
                else -> {
                    val idx = AccuracyRanges.indexFor(accuracy)
                    if (idx == null) "Assertividade inválida."
                    else {
                        val gales = strategyPrefs.getInt("gale_$idx", defaults[idx])
                        val mult = strategyPrefs.getString("mult_$idx", "2")
                        "Faixa ${starts[idx]}–${limits[idx]}%: até $gales Gales ×$mult (prévia local)"
                    }
                }
            }
        }
        val changeWatcher = object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                if (!loadingRanges) dirtyRanges = true
            }
            override fun afterTextChanged(s: Editable?) {}
        }
        (galeFields + multFields).forEach { it.addTextChangedListener(changeWatcher) }
        toggle.setOnCheckedChangeListener { _, checked ->
            if (!loadingRanges) dirtyRanges = true
            strategyPrefs.edit().putBoolean("enabled", checked).apply()
            updateRange()
        }
        val saveRanges = Button(this).apply { text = "SALVAR FAIXAS" }
        rules.addView(saveRanges)
        saveRanges.setOnClickListener {
            val gales = galeFields.map { it.text.toString().toIntOrNull() }
            val multipliers = multFields.map { it.text.toString().replace(',', '.').toDoubleOrNull() }
            if (gales.any { it == null || it !in 0..9 } || multipliers.any { it == null || !it.isFinite() || it < 1.0 || it > 10.0 }) {
                Toast.makeText(this, "Gales: 0 a 9; multiplicador: 1 a 10.", Toast.LENGTH_LONG).show()
            } else {
                val editor = strategyPrefs.edit().putBoolean("enabled", toggle.isChecked)
                for (i in starts.indices) {
                    editor.putInt("gale_$i", gales[i]!!)
                    editor.putString("mult_$i", multipliers[i]!!.toString())
                }
                editor.apply()
                dirtyRanges = true
                updateRange()
                Toast.makeText(this, "Faixas salvas apenas neste aplicativo.", Toast.LENGTH_SHORT).show()
            }
        }
        rules.addView(text("As faixas são aplicadas pelo robô PRACTICE em novas operações após a confirmação de recebimento. Operações em andamento mantêm suas regras.", 12f))
        updateRange()
        addPanel(root, rules)

        val connect = panel()
        connect.addView(text("CONEXÃO COM RENDER", 17f, true))
        endpoint = EditText(this).apply {
            hint = "https://baskaraido-monitor-test.onrender.com"
            setSingleLine(true)
            setTextColor(ink); setHintTextColor(muted)
            setText(getPreferences(0).getString("endpoint", "https://baskaraido-monitor-test.onrender.com"))
        }
        connect.addView(endpoint)
        tokenField = EditText(this).apply {
            hint = "Token de monitoramento (não é a senha IQ)"
            setSingleLine(true)
            inputType = 129
            isSaveEnabled = false
            importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
            setTextColor(ink); setHintTextColor(muted)
        }
        connect.addView(tokenField)
        val button = Button(this).apply { text = "TESTAR CONEXÃO"; backgroundTintList = android.content.res.ColorStateList.valueOf(Color.rgb(56, 214, 255)); setTextColor(Color.rgb(6, 20, 42)) }
        connect.addView(button)
        val connectionResult = text("Toque em TESTAR CONEXÃO para verificar o servidor.", 13f, true).apply { setTextColor(Color.rgb(105, 240, 195)) }
        connect.addView(connectionResult)
        connect.addView(text("Use apenas o token exclusivo de monitoramento. Nunca informe sua senha da IQ Option ou o token do Telegram.", 12f))
        val syncButton = Button(this).apply { text = "ENVIAR FAIXAS AO AMBIENTE DE TESTES" }
        connect.addView(syncButton)
        val syncResult = text("Conecte para carregar a revisão das faixas do servidor.", 12f)
        connect.addView(syncResult)
        fun showConfig(config: PracticeProtocol.Config, populate: Boolean) {
            val revisionChangedWhileEditing = dirtyRanges && remoteConfig != null &&
                remoteConfig!!.configId != config.configId
            if (!dirtyRanges || remoteConfig == null) remoteConfig = config
            if (populate) {
                loadingRanges = true
                toggle.isChecked = config.enabled
                val editor = strategyPrefs.edit().putBoolean("enabled", config.enabled)
                config.bands.forEachIndexed { i, band ->
                    galeFields[i].setText(band.gales.toString())
                    multFields[i].setText(band.multiplier.toString())
                    editor.putInt("gale_$i", band.gales).putString("mult_$i", band.multiplier.toString())
                }
                editor.apply()
                loadingRanges = false
                dirtyRanges = false
                updateRange()
            }
            val applied = config.appliedRevision == config.revision && config.appliedConfigId == config.configId
            syncResult.text = "Revisão ${config.revision}: " +
                (if (applied) "recebida pelo robô; somente novas operações." else "aguardando confirmação do robô PRACTICE.") +
                (if (revisionChangedWhileEditing) " A revisão mudou durante a edição; reconecte para revisar." else
                    if (dirtyRanges) " Há alterações locais não enviadas." else "")
        }
        fun markStale() {
            currentAccuracy = null
            dataOrigin.text = "DADOS DESATUALIZADOS — aguardando atualização autenticada"
            updateRange()
        }
        fun schedulePoll() {
            poll?.let(handler::removeCallbacks)
            if (foreground && session != null) {
                poll = Runnable { refreshAction?.invoke() }
                handler.postDelayed(poll!!, 5000)
            }
        }
        fun render(snapshot: PracticeProtocol.Snapshot) {
            val robotData = snapshot.robotData
            val testData = !robotData
            dataOrigin.text = when {
                snapshot.stale -> "DADOS DESATUALIZADOS — ROBÔ PRACTICE SEM ATUALIZAÇÃO RECENTE"
                robotData -> "ROBÔ PRACTICE — CONTADORES REINICIAM COM O PROCESSO"
                else -> "DADOS SIMULADOS — NÃO SÃO OPERAÇÕES REAIS"
            }
            bankHint.text = if (testData) "Bancas simuladas para validar a conexão." else "Bancas do robô PRACTICE; saldo — quando não verificado."
            summaryStats.text = "WIN ${snapshot.wins}        LOSS ${snapshot.losses}"
            accuracyStats.text = "ASSERTIVIDADE ${String.format(Locale.getDefault(), "%.2f", snapshot.accuracy)}%"
            currentAccuracy = if (robotData && !snapshot.stale) snapshot.accuracy else null
            snapshot.banks.forEachIndexed { i, bank ->
                val balance = bank.balance?.let { String.format(Locale.getDefault(), "%.2f", it) } ?: "—"
                (banks.getChildAt(i) as TextView).text = "●   BANCA %02d       SALDO %s       W %d / L %d".format(i + 1, balance, bank.wins, bank.losses)
            }
            updateRange()
        }
        refreshAction = refresh@{
            val auth = session ?: return@refresh
            if (!foreground || activeTask?.isDone == false) return@refresh
            val requestGeneration = generation
            activeTask = worker.submit {
                try {
                    val snapshot = PracticeProtocol.monitor(request(auth.first, auth.second, "/api/v1/monitor"))
                    val config = PracticeProtocol.config(request(auth.first, auth.second, "/api/v1/practice/faixas"))
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            render(snapshot)
                            showConfig(config, !dirtyRanges)
                            status.text = if (snapshot.stale) "Robô sem atualização recente." else "Conectado: atualização automática a cada 5 segundos."
                            connectionResult.text = status.text
                            button.isEnabled = true
                            syncButton.isEnabled = true
                            (galeFields + multFields).forEach { it.isEnabled = true }
                            toggle.isEnabled = true
                            schedulePoll()
                        }
                    }
                } catch (error: Exception) {
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            markStale()
                            status.text = failureMessage(error)
                            connectionResult.text = status.text
                            button.isEnabled = true
                            syncButton.isEnabled = true
                            (galeFields + multFields).forEach { it.isEnabled = true }
                            toggle.isEnabled = true
                            if (error is ApiFailure && error.code == 401) session = null
                            schedulePoll()
                        }
                    }
                }
            }
        }
        syncButton.setOnClickListener {
            if (activeTask?.isDone == false) {
                syncResult.text = "Aguarde a atualização em andamento e envie novamente."
                return@setOnClickListener
            }
            val auth = try {
                val base = endpoint.text.toString().trim()
                PracticeProtocol.address(base, "/api/v1/practice/faixas")
                Pair(base, PracticeProtocol.token(tokenField.text.toString()))
            } catch (_: Exception) {
                syncResult.text = "Use o endereço HTTPS do monitor isolado e o token de monitoramento."
                return@setOnClickListener
            }
            val config = remoteConfig
            if (config == null || session != auth) {
                syncResult.text = "Teste a conexão para carregar a revisão atual antes de enviar."
                return@setOnClickListener
            }
            val payload = try {
                val bands = starts.indices.map { i -> PracticeProtocol.Band(
                    galeFields[i].text.toString().toInt(),
                    multFields[i].text.toString().replace(',', '.').toDouble()) }
                PracticeProtocol.payload(toggle.isChecked, bands, config.revision, config.configId)
            } catch (_: Exception) {
                syncResult.text = "Corrija os Gales (0–9) e multiplicadores (1–10)."
                return@setOnClickListener
            }
            poll?.let(handler::removeCallbacks)
            syncButton.isEnabled = false
            (galeFields + multFields).forEach { it.isEnabled = false }
            toggle.isEnabled = false
            syncResult.text = "Enviando ao monitor de testes..."
            val requestGeneration = generation
            activeTask = worker.submit {
                try {
                    val saved = PracticeProtocol.config(request(auth.first, auth.second, "/api/v1/practice/faixas", payload))
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            showConfig(saved, true)
                            syncButton.isEnabled = true
                            (galeFields + multFields).forEach { it.isEnabled = true }
                            toggle.isEnabled = true
                            schedulePoll()
                        }
                    }
                } catch (error: Exception) {
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            syncResult.text = failureMessage(error)
                            syncButton.isEnabled = true
                            (galeFields + multFields).forEach { it.isEnabled = true }
                            toggle.isEnabled = true
                            if (error is ApiFailure && error.code == 409) {
                                remoteConfig = null
                                session = null
                                markStale()
                            }
                            if (error is ApiFailure && error.code == 401) session = null
                            schedulePoll()
                        }
                    }
                }
            }
        }

        addPanel(root, connect)

        button.setOnClickListener {
            val auth = try {
                val base = endpoint.text.toString().trim()
                PracticeProtocol.address(base, "/api/v1/monitor")
                Pair(base, PracticeProtocol.token(tokenField.text.toString()))
            } catch (_: Exception) {
                status.text = "Use o endereço HTTPS do monitor isolado e o token de monitoramento."
                connectionResult.text = status.text
                return@setOnClickListener
            }
            generation++
            poll?.let(handler::removeCallbacks)
            activeTask?.cancel(true)
            connection?.disconnect()
            activeTask = null
            remoteConfig = null
            session = auth
            getPreferences(0).edit().putString("endpoint", auth.first).apply()
            status.text = "Verificando servidor..."
            connectionResult.text = status.text
            button.isEnabled = false
            refreshAction?.invoke()
        }
    }
}
