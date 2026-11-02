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
    private val savedCredentials by lazy { SecureMonitorCredentials(this) }
    private lateinit var tokenField: EditText
    private lateinit var controlTokenField: EditText
    private lateinit var iqStats: TextView
    private lateinit var recoveryStats: TextView
    private lateinit var operationStats: TextView
    private lateinit var controlStatus: TextView
    private lateinit var pauseButton: Button
    private lateinit var resumeButton: Button
    private val confirmation = PracticeProtocol.Confirmation()
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
        confirmation.failed()
        if (::pauseButton.isInitialized) {
            pauseButton.isEnabled = false; resumeButton.isEnabled = false
            controlStatus.text = "Estado efetivo sem confirmação recente. Atualizando ao retornar ao aplicativo."
        }
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
            if (conn.responseCode != 200 && !(payload != null && conn.responseCode == 202)) throw ApiFailure(conn.responseCode)
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
        error is ApiFailure && error.code in setOf(401, 403) -> "Token rejeitado. Informe um token válido e reconecte."
        error is ApiFailure && error.code == 409 -> "Revisão alterada ou comando repetido. Atualize e revise antes de enviar."
        error is ApiFailure && error.code == 503 -> "Controle indisponível no servidor. Nenhuma confirmação recebida."
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
        root.addView(text("Controle de novas entradas · somente PRACTICE", 12f))

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
        iqStats = text("IQ OPTION — conexão e saldo PRACTICE não confirmados", 13f)
        summary.addView(iqStats)
        recoveryStats = text("RECUPERAÇÃO — aguardando dados do robô", 13f)
        summary.addView(recoveryStats)
        operationStats = text("ATIVOS E OPERAÇÕES — aguardando dados", 12f)
        summary.addView(operationStats)
        summary.addView(text("Recuperação planejada pode não ser atingida; depende de payout, saldo e resultados.", 12f))
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
        val minFields = mutableListOf<EditText>()
        val maxFields = mutableListOf<EditText>()
        rules.addView(text("Mínimo %  ·  Máximo %  ·  Gales  ·  Multiplicador", 11f))
        for (i in starts.indices) {
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
            val lower = EditText(this).apply {
                setSingleLine(true); inputType = InputType.TYPE_CLASS_NUMBER
                setText(strategyPrefs.getInt("min_$i", starts[i]).toString())
                setTextColor(ink); hint = "Mín"
                contentDescription = "Limite mínimo percentual da faixa ${i + 1}"
            }
            val upper = EditText(this).apply {
                setSingleLine(true); inputType = InputType.TYPE_CLASS_NUMBER
                setText(strategyPrefs.getInt("max_$i", limits[i]).toString())
                setTextColor(ink); hint = "Máx"
                contentDescription = "Limite máximo percentual da faixa ${i + 1}"
            }
            minFields.add(lower); maxFields.add(upper)
            row.addView(lower, LinearLayout.LayoutParams(0, -2, 1f))
            row.addView(upper, LinearLayout.LayoutParams(0, -2, 1f))
            val gale = EditText(this).apply {
                setSingleLine(true); inputType = InputType.TYPE_CLASS_NUMBER
                setText(strategyPrefs.getInt("gale_$i", defaults[i]).toString())
                setTextColor(ink); hint = "Gales"
                contentDescription = "Quantidade de Gales na faixa ${i + 1}"
            }
            row.addView(gale, LinearLayout.LayoutParams(dp(60), -2))
            galeFields.add(gale)
            val mult = EditText(this).apply {
                setSingleLine(true); inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
                setText(strategyPrefs.getString("mult_$i", "2"))
                setTextColor(ink); hint = "×"
                contentDescription = "Multiplicador na faixa ${i + 1}"
            }
            row.addView(mult, LinearLayout.LayoutParams(dp(65), -2))
            multFields.add(mult)
            rules.addView(row)
        }
        rules.addView(text("Use cinco faixas consecutivas, sem lacunas: de 0% até 100%.", 12f))
        fun readBands() = PracticeProtocol.validatedBands(starts.indices.map { i -> PracticeProtocol.Band(
            galeFields[i].text.toString().toInt(), multFields[i].text.toString().replace(',', '.').toDouble(),
            minFields[i].text.toString().toInt(), maxFields[i].text.toString().toInt()) })
        rangeResult = text("Faixa ainda não calculada.", 13f, true)
        rules.addView(rangeResult)
        fun updateRange() {
            val accuracy = currentAccuracy
            rangeResult.text = when {
                !toggle.isChecked -> "Gestão por faixas desativada."
                accuracy == null -> "Aguardando assertividade do monitor PRACTICE."
                else -> {
                    try {
                        val saved = starts.indices.map { i -> PracticeProtocol.Band(
                            strategyPrefs.getInt("gale_$i", defaults[i]),
                            strategyPrefs.getString("mult_$i", "2")!!.toDouble(),
                            strategyPrefs.getInt("min_$i", starts[i]), strategyPrefs.getInt("max_$i", limits[i])) }
                        val idx = PracticeProtocol.bandIndex(accuracy, saved)
                        if (idx == null) "Assertividade inválida."
                        else saved[idx].let { band ->
                            "Faixa ${band.min}–${band.max}%: até ${band.gales} Gales ×${band.multiplier} (prévia local)"
                        }
                    } catch (_: Exception) { "Revise os limites e valores das cinco faixas." }
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
        (galeFields + multFields + minFields + maxFields).forEach { it.addTextChangedListener(changeWatcher) }
        toggle.setOnCheckedChangeListener { _, checked ->
            if (!loadingRanges) dirtyRanges = true
            strategyPrefs.edit().putBoolean("enabled", checked).apply()
            updateRange()
        }
        val saveRanges = Button(this).apply { text = "SALVAR RASCUNHO LOCAL" }
        rules.addView(saveRanges)
        saveRanges.setOnClickListener {
            val bands = try { readBands() } catch (_: Exception) {
                Toast.makeText(this, "Faixas consecutivas 0–100%; Gales 0–9; multiplicador 1–10.", Toast.LENGTH_LONG).show()
                return@setOnClickListener
            }
            run {
                val editor = strategyPrefs.edit().putBoolean("enabled", toggle.isChecked)
                for (i in starts.indices) {
                    editor.putInt("gale_$i", bands[i].gales)
                    editor.putString("mult_$i", bands[i].multiplier.toString())
                    editor.putInt("min_$i", bands[i].min!!).putInt("max_$i", bands[i].max!!)
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
        savedCredentials.load()?.let { tokenField.setText(it) }
        controlTokenField = EditText(this).apply {
            hint = "Token separado de controle PRACTICE"
            setSingleLine(true); inputType = 129
            isSaveEnabled = false
            importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
            setTextColor(ink); setHintTextColor(muted)
        }
        connect.addView(controlTokenField)
        val button = Button(this).apply { text = "TESTAR CONEXÃO"; backgroundTintList = android.content.res.ColorStateList.valueOf(Color.rgb(56, 214, 255)); setTextColor(Color.rgb(6, 20, 42)) }
        connect.addView(button)
        val connectionResult = text("Toque em TESTAR CONEXÃO para verificar o servidor.", 13f, true).apply { setTextColor(Color.rgb(105, 240, 195)) }
        connect.addView(connectionResult)
        connect.addView(text("Token de leitura protegido pelo Android Keystore e reconexão automática. O token de controle permanece apenas nesta sessão. Nunca informe senha IQ, token do robô ou Telegram.", 12f))
        val forgetTokenButton = Button(this).apply { text = "ESQUECER TOKEN SALVO" }
        connect.addView(forgetTokenButton)
        forgetTokenButton.setOnClickListener {
            savedCredentials.clear()
            tokenField.text.clear()
            session = null
            generation++
            poll?.let(handler::removeCallbacks)
            activeTask?.cancel(true)
            connection?.disconnect()
            pauseButton.isEnabled = false; resumeButton.isEnabled = false
            status.text = "Token removido. Informe um novo token para conectar."
            connectionResult.text = status.text
        }
        val syncButton = Button(this).apply { text = "SALVAR FAIXAS NO ROBÔ PRACTICE" }
        connect.addView(syncButton)
        val syncResult = text("Conecte para carregar a revisão das faixas do servidor.", 12f)
        connect.addView(syncResult)
        pauseButton = Button(this).apply { text = "PAUSAR NOVAS ENTRADAS"; isEnabled = false }
        resumeButton = Button(this).apply { text = "LIBERAR NOVAS ENTRADAS"; isEnabled = false }
        connect.addView(pauseButton); connect.addView(resumeButton)
        controlStatus = text("Estado efetivo não confirmado. Conecte para consultar o robô.", 13f, true)
        connect.addView(controlStatus)
        connect.addView(text("A liberação exige autorização prévia do robô. Pausa afeta novas entradas; operações e Gales em andamento continuam.", 12f))
        fun showControl(control: PracticeProtocol.Control) {
            confirmation.observed(control)
            controlStatus.text = when {
                !control.online -> "Robô sem confirmação recente. Estado efetivo desconhecido."
                control.pending -> "Comando revisão ${control.revision} pendente de confirmação pelo robô."
                !control.confirmed -> "Estado efetivo não confirmado pelo robô."
                control.paused == true -> "PAUSA CONFIRMADA pelo robô · revisão ${control.revision}."
                !control.authorized -> "Robô não autorizado a enviar ordens. Liberação indisponível."
                else -> "NOVAS ENTRADAS LIBERADAS pelo robô · revisão ${control.revision}."
            } + (control.error?.let { " Erro: $it" } ?: "")
            pauseButton.isEnabled = confirmation.canPause
            resumeButton.isEnabled = confirmation.canResume
        }
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
                    minFields[i].setText(band.min.toString()); maxFields[i].setText(band.max.toString())
                    editor.putInt("gale_$i", band.gales).putString("mult_$i", band.multiplier.toString())
                    editor.putInt("min_$i", band.min!!).putInt("max_$i", band.max!!)
                }
                editor.apply()
                loadingRanges = false
                dirtyRanges = false
                updateRange()
            }
            val applied = !config.pending && config.appliedRevision == config.revision && config.appliedConfigId == config.configId
            syncResult.text = "Revisão ${config.revision}: " +
                (if (applied) "confirmada pelo robô; somente novas operações." else "aguardando confirmação do robô PRACTICE.") +
                (if (revisionChangedWhileEditing) " A revisão mudou durante a edição; reconecte para revisar." else
                    if (dirtyRanges) " Há alterações locais não enviadas." else "") +
                (config.effectiveBands?.let { effective -> "\nEfetivo (revisão ${config.appliedRevision}): gestão ${if (config.effectiveEnabled == true) "ativada" else "desativada"}. " + effective.mapIndexed { i, band ->
                    "${band.min}–${band.max}%: ${band.gales}G ×${band.multiplier}"
                }.joinToString(" · ") } ?: "\nConfiguração efetiva ainda não informada pelo robô.")
        }
        fun markStale() {
            currentAccuracy = null
            confirmation.failed()
            pauseButton.isEnabled = false; resumeButton.isEnabled = false
            controlStatus.text = "Conexão indisponível. Comando e estado não confirmados; atualize para reconciliar."
            syncResult.text = "Confirmação indisponível. A última configuração exibida pode estar desatualizada."
            iqStats.text = "IQ OPTION — conexão e saldo sem confirmação recente"
            recoveryStats.text = "RECUPERAÇÃO — estado sem confirmação recente; aguardando dados do robô."
            operationStats.text = "ATIVOS E OPERAÇÕES — estado sem confirmação recente."
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
            fun amount(value: Double?): String = value?.let { String.format(Locale.getDefault(), "%.2f", it) } ?: "—"
            iqStats.text = "IQ OPTION: " + when (snapshot.iqConnected) {
                true -> "conectada"; false -> "desconectada"; null -> "não informada"
            } + " · Saldo PRACTICE: ${amount(snapshot.practiceBalance)}" + if (snapshot.stale) " (desatualizado)" else ""
            recoveryStats.text = snapshot.recovery?.let { r ->
                "PREJUÍZO GLOBAL ${amount(r.loss)} · recuperação planejada ${amount(r.planned)}\n" +
                "Entrada planejada ${amount(r.stake)} · ${when (r.state) {
                    "sem_prejuizo" -> "sem prejuízo acumulado"
                    "aguardando_operacao" -> "aguardando operação elegível"
                    "saldo_insuficiente" -> "saldo insuficiente"
                    "em_andamento" -> "em andamento"
                    "envio_indeterminado" -> "envio sem confirmação; entradas bloqueadas"
                    else -> "estado não informado"
                }} ${r.bank ?: ""}\n" +
                "Próximos Gales: ${r.gales ?: "—"} · ×${r.multiplier ?: "—"}"
            } ?: "RECUPERAÇÃO — não informada pelo robô"
            operationStats.text = "ATIVOS: ${snapshot.assets.joinToString(", ").ifEmpty { "—" }}\n" +
                "OPERAÇÕES: ${snapshot.operations.joinToString("\n").ifEmpty { "nenhuma informada" }}"
            snapshot.banks.forEachIndexed { i, bank ->
                if (bank.asset != null || bank.gales != null) (banks.getChildAt(i) as TextView).append(
                    "\n${bank.asset ?: "—"} · ${bank.gales ?: "—"}G ×${bank.multiplier ?: "—"}" +
                        if (bank.recovering == true) " · recuperação" else "")
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
                    savedCredentials.save(auth.second)
                    val config = PracticeProtocol.config(request(auth.first, auth.second, "/api/v1/practice/settings"))
                    val control = PracticeProtocol.control(request(auth.first, auth.second, "/api/v1/practice/control"))
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            render(snapshot)
                            showConfig(config, !dirtyRanges)
                            showControl(control)
                            status.text = if (snapshot.stale) "Robô sem atualização recente." else "Conectado: atualização automática a cada 5 segundos."
                            connectionResult.text = status.text
                            button.isEnabled = true
                            syncButton.isEnabled = true
                            (galeFields + multFields + minFields + maxFields).forEach { it.isEnabled = true }
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
                            (galeFields + multFields + minFields + maxFields).forEach { it.isEnabled = true }
                            toggle.isEnabled = true
                            if (error is ApiFailure && error.code == 401) {
                                session = null
                                savedCredentials.clear()
                            }
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
                PracticeProtocol.address(base, "/api/v1/practice/settings")
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
                val bands = readBands()
                PracticeProtocol.settingsPayload(toggle.isChecked, bands, config.revision, config.configId)
            } catch (_: Exception) {
                syncResult.text = "Use faixas consecutivas 0–100%, Gales 0–9 e multiplicadores 1–10."
                return@setOnClickListener
            }
            val writeToken = try { PracticeProtocol.token(controlTokenField.text.toString()).also {
                require(it != auth.second)
            } } catch (_: Exception) {
                syncResult.text = "Informe um token de controle diferente do token de leitura."
                return@setOnClickListener
            }
            poll?.let(handler::removeCallbacks)
            syncButton.isEnabled = false
            (galeFields + multFields + minFields + maxFields).forEach { it.isEnabled = false }
            toggle.isEnabled = false
            syncResult.text = "Enviando configuração; aplicação pelo robô ainda não confirmada..."
            val requestGeneration = generation
            activeTask = worker.submit {
                try {
                    val saved = PracticeProtocol.config(request(auth.first, writeToken, "/api/v1/practice/settings", payload))
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            showConfig(saved, true)
                            syncButton.isEnabled = true
                            (galeFields + multFields + minFields + maxFields).forEach { it.isEnabled = true }
                            toggle.isEnabled = true
                            schedulePoll()
                        }
                    }
                } catch (error: Exception) {
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            markStale()
                            syncResult.text = failureMessage(error) + " Configuração não confirmada; consulte novamente antes de reenviar."
                            syncButton.isEnabled = true
                            (galeFields + multFields + minFields + maxFields).forEach { it.isEnabled = true }
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

        fun sendControl(action: String) {
            if (activeTask?.isDone == false) return
            val auth = session ?: return
            val observed = confirmation.control ?: return
            if (action == "resume_new_entries" && !confirmation.canResume) return
            if (action == "pause_new_entries" && !confirmation.canPause) return
            val writeToken = try {
                PracticeProtocol.token(controlTokenField.text.toString()).also { require(it != auth.second) }
            } catch (_: Exception) {
                controlStatus.text = "Informe um token de controle diferente do token de leitura."
                return
            }
            val payload = PracticeProtocol.command(action, observed.revision)
            confirmation.begin()
            pauseButton.isEnabled = false; resumeButton.isEnabled = false
            poll?.let(handler::removeCallbacks)
            controlStatus.text = "Enviando comando; aguardando confirmação efetiva do robô..."
            val requestGeneration = generation
            activeTask = worker.submit {
                try {
                    // Even HTTP 202 only queues a command. A later authenticated GET must confirm it.
                    PracticeProtocol.control(request(auth.first, writeToken, "/api/v1/practice/control", payload))
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            controlStatus.text = "Comando recebido pelo servidor. Aguardando confirmação do robô."
                            schedulePoll()
                        }
                    }
                } catch (error: Exception) {
                    runOnUiThread {
                        if (foreground && generation == requestGeneration) {
                            markStale()
                            controlStatus.text = failureMessage(error) + " Comando não confirmado; consulte o estado antes de reenviar."
                            schedulePoll()
                        }
                    }
                }
            }
        }
        pauseButton.setOnClickListener { sendControl("pause_new_entries") }
        resumeButton.setOnClickListener { sendControl("resume_new_entries") }
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
            confirmation.begin()
            pauseButton.isEnabled = false; resumeButton.isEnabled = false
            session = auth
            getPreferences(0).edit().putString("endpoint", auth.first).apply()
            status.text = "Verificando servidor..."
            connectionResult.text = status.text
            button.isEnabled = false
            refreshAction?.invoke()
        }
        if (savedCredentials.load() != null) button.post { if (foreground) button.performClick() }
    }
}

