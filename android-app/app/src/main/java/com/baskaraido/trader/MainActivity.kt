package com.baskaraido.trader

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
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
            setColor(cardColor); cornerRadius = dp(18).toFloat()
            setStroke(dp(1), Color.argb(90, 210, 220, 255))
        }
        setPadding(dp(16), dp(12), dp(16), dp(12))
    }
    private fun addPanel(parent: LinearLayout, view: View) {
        parent.addView(view, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(12) })
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val scroll = ScrollView(this).apply { isFillViewport = true }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(26), dp(18), dp(30))
            setBackgroundColor(Color.rgb(10, 17, 37))
        }
        scroll.addView(root)
        setContentView(scroll)

        root.addView(text("BASKARAIDO TRADER PRO", 25f, true))
        val practice = text("● MONITORAMENTO  •  PRACTICE", 13f, true)
        practice.setTextColor(Color.rgb(105, 240, 195))
        root.addView(practice)
        root.addView(text("Painel de acompanhamento · sem envio de ordens", 12f))

        val summary = panel()
        summary.addView(text("VISÃO GERAL", 13f, true))
        status = text("Aguardando conexão autenticada com o Render.", 14f)
        summary.addView(status)
        summary.addView(text("WIN: —     LOSS: —     ASSERTIVIDADE: —", 15f, true))
        addPanel(root, summary)

        val bankPanel = panel()
        bankPanel.addView(text("10 BANCAS", 18f, true))
        bankPanel.addView(text("Valores indisponíveis até conectar a API segura.", 12f))
        banks = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        for (i in 1..10) {
            val item = text("Banca %02d    •    saldo —    •    WIN — / LOSS —".format(i), 13f)
            item.setTextColor(muted)
            banks.addView(item)
        }
        bankPanel.addView(banks)
        addPanel(root, bankPanel)

        val rules = panel()
        rules.addView(text("GESTÃO DE ASSERTIVIDADE", 17f, true))
        rules.addView(text("Abaixo de 60%: até 2 Gales ×2", 14f))
        rules.addView(text("A partir de 60%: sem Gale", 14f))
        rules.addView(text("Indicadores e Telegram preservados no robô Python.", 12f))
        addPanel(root, rules)

        val connect = panel()
        connect.addView(text("CONEXÃO COM RENDER", 17f, true))
        endpoint = EditText(this).apply {
            hint = "https://seu-servico.onrender.com"
            setSingleLine(true)
            setTextColor(ink); setHintTextColor(muted)
            setText(getPreferences(0).getString("endpoint", ""))
        }
        connect.addView(endpoint)
        val button = Button(this).apply { text = "TESTAR CONEXÃO" }
        connect.addView(button)
        connect.addView(text("A consulta requer uma API de monitoramento autenticada. Não insira senhas ou tokens aqui.", 12f))
        addPanel(root, connect)

        button.setOnClickListener {
            val base = endpoint.text.toString().trim().trimEnd('/')
            val url = try { URL(base) } catch (_: Exception) { null }
            if (url == null || url.protocol != "https" || url.host.isBlank()) {
                status.text = "Informe um endereço HTTPS válido."
                return@setOnClickListener
            }
            getPreferences(0).edit().putString("endpoint", base).apply()
            status.text = "Verificando servidor..."
            Thread {
                val message = try {
                    val conn = (url.openConnection() as HttpURLConnection).apply {
                        connectTimeout = 8000; readTimeout = 8000; requestMethod = "GET"
                        instanceFollowRedirects = false
                    }
                    try {
                        if (conn.responseCode != 200) "Servidor respondeu HTTP ${conn.responseCode}. API ainda não conectada."
                        else {
                            val body = conn.inputStream.bufferedReader().use { it.readText().take(3000) }
                            val json = JSONObject(body)
                            val account = json.optString("conta", json.optString("account", ""))
                            if (account.equals("PRACTICE", true))
                                "Servidor respondeu PRACTICE. Integração de dados das bancas ainda pendente."
                            else "Servidor acessível, mas conta PRACTICE não confirmada. Dados não exibidos."
                        }
                    } finally { conn.disconnect() }
                } catch (e: Exception) { "Conexão não concluída: ${e.javaClass.simpleName}" }
                runOnUiThread { status.text = message }
            }.start()
        }
    }
}
