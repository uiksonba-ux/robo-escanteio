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
        summary.addView(text("WIN     —        LOSS     —", 18f, true))
        summary.addView(text("ASSERTIVIDADE     —", 16f, true).apply { setTextColor(Color.rgb(56, 214, 255)) })
        addPanel(root, summary)

        val bankPanel = panel()
        bankPanel.addView(text("10 BANCAS", 18f, true))
        bankPanel.addView(text("Valores indisponíveis até conectar a API segura.", 12f))
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
        val button = Button(this).apply { text = "TESTAR CONEXÃO"; backgroundTintList = android.content.res.ColorStateList.valueOf(Color.rgb(56, 214, 255)); setTextColor(Color.rgb(6, 20, 42)) }
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
