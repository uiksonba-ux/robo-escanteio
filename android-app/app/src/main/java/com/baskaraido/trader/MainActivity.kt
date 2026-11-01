package com.baskaraido.trader

import android.app.Activity
import android.graphics.Color
import android.graphics.BitmapFactory
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.LayerDrawable
import android.graphics.drawable.ColorDrawable
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

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
        rules.addView(text("Configuração local • não altera operações do robô", 12f))
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
        toggle.setOnCheckedChangeListener { _, checked ->
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
                updateRange()
                Toast.makeText(this, "Faixas salvas apenas neste aplicativo.", Toast.LENGTH_SHORT).show()
            }
        }
        rules.addView(text("Esta tela apenas calcula a faixa. Não envia ordens nem modifica Telegram ou estratégia ativa.", 12f))
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
            setTextColor(ink); setHintTextColor(muted)
        }
        connect.addView(tokenField)
        val button = Button(this).apply { text = "TESTAR CONEXÃO"; backgroundTintList = android.content.res.ColorStateList.valueOf(Color.rgb(56, 214, 255)); setTextColor(Color.rgb(6, 20, 42)) }
        connect.addView(button)
        val connectionResult = text("Toque em TESTAR CONEXÃO para verificar o servidor.", 13f, true).apply { setTextColor(Color.rgb(105, 240, 195)) }
        connect.addView(connectionResult)
        connect.addView(text("Use apenas o token exclusivo de monitoramento. Nunca informe sua senha da IQ Option ou o token do Telegram.", 12f))
        addPanel(root, connect)

        button.setOnClickListener {
            fun showResult(message: String) { status.text = message; connectionResult.text = message }
            val base = endpoint.text.toString().trim().trimEnd('/')
            val url = try { URL(base + "/api/v1/monitor") } catch (_: Exception) { null }
            val token = tokenField.text.toString().trim()
            if (url == null || url.protocol != "https" || url.host.isBlank()) {
                showResult("Informe um endereço HTTPS válido.")
                return@setOnClickListener
            }
            if (token.isEmpty()) {
                showResult("Informe o token de monitoramento.")
                return@setOnClickListener
            }
            getPreferences(0).edit().putString("endpoint", base).apply()
            showResult("Verificando servidor...")
            button.isEnabled = false
            Thread {
                val message = try {
                    val conn = (url.openConnection() as HttpURLConnection).apply {
                        connectTimeout = 30000; readTimeout = 30000; requestMethod = "GET"
                        instanceFollowRedirects = false
                        setRequestProperty("Authorization", "Bearer $token")
                    }
                    try {
                        if (conn.responseCode != 200) "API respondeu HTTP ${conn.responseCode}."
                        else {
                            val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText().take(30000) })
                            if (json.optString("conta") != "PRACTICE" || json.optString("modo") != "somente_leitura" || json.optInt("schema_version") != 1) {
                                "Resposta rejeitada: somente PRACTICE e leitura são permitidos."
                            } else {
                                val totals = json.getJSONObject("estatisticas")
                                val entries = json.getJSONObject("bancas")
                                val origin = json.optString("origem")
                                val testData = origin == "ambiente_de_teste_sem_dados_reais"
                                val robotData = origin == "robo_practice_memoria_volatil"
                                val originLabel = when {
                                    testData -> "DADOS SIMULADOS — NÃO SÃO OPERAÇÕES REAIS"
                                    robotData -> "ROBÔ PRACTICE — CONTADORES REINICIAM COM O PROCESSO"
                                    else -> "ORIGEM DESCONHECIDA — DADOS NÃO CONFIÁVEIS"
                                }
                                runOnUiThread {
                                    dataOrigin.text = originLabel
                                    bankHint.text = when { testData -> "Bancas simuladas para validar a conexão."; robotData -> "Bancas do processo atual do robô PRACTICE."; else -> "Origem não reconhecida; não use estes dados." }
                                    summaryStats.text = "WIN ${totals.getInt("wins")}        LOSS ${totals.getInt("losses")}"
                                    accuracyStats.text = "ASSERTIVIDADE ${totals.getDouble("assertividade")}%"
                                    currentAccuracy = if (robotData) totals.getDouble("assertividade") else null\n                                    updateRange()
                                    for (i in 1..10) {
                                        val bank = entries.optJSONObject("BANCA $i")
                                        val w = bank?.optInt("wins", 0) ?: 0
                                        val l = bank?.optInt("losses", 0) ?: 0
                                        (banks.getChildAt(i - 1) as TextView).text = "●   BANCA %02d       SALDO —       W %d / L %d".format(i, w, l)
                                    }
                                }
                                when { testData -> "Conectado: dados simulados, sem operações reais."; robotData -> "Conectado: robô PRACTICE (memória temporária)."; else -> "Conectado, mas origem dos dados desconhecida." }
                            }
                        }
                    } finally { conn.disconnect() }
                } catch (e: Exception) { "Conexão não concluída: ${e.javaClass.simpleName}" }
                runOnUiThread { showResult(message); button.isEnabled = true }
            }.start()
        }
    }
}
