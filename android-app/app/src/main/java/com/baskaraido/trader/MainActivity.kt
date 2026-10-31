package com.baskaraido.trader

import android.app.Activity
import android.os.Bundle
import android.graphics.Color
import android.widget.*
import android.view.ViewGroup
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : Activity() {
 override fun onCreate(savedInstanceState: Bundle?) {
  super.onCreate(savedInstanceState)
  val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(36,48,36,24); setBackgroundColor(Color.rgb(13,22,38)) }
  fun label(value: String, size: Float = 17f): TextView = TextView(this).apply { text=value; textSize=size; setTextColor(Color.WHITE); setPadding(0,12,0,12) }
  root.addView(label("BASKARAIDO TRADER", 26f))
  root.addView(label("MONITORAMENTO • PRACTICE", 16f))
  val endpoint = EditText(this).apply { hint="https://seu-servico.onrender.com"; setSingleLine(true); setTextColor(Color.WHITE); setHintTextColor(Color.LTGRAY); setText(getPreferences(0).getString("endpoint","")) }
  root.addView(endpoint)
  val output = label("Configure a URL HTTPS do seu Render para consultar o status.")
  root.addView(output)
  val button = Button(this).apply { text="Atualizar status" }
  root.addView(button, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
  root.addView(label("Somente leitura. Este APK não envia ordens nem armazena senha da corretora.", 13f))
  setContentView(root)
  button.setOnClickListener {
   val base=endpoint.text.toString().trim().trimEnd('/')
   if (!base.startsWith("https://")) { output.text="Use uma URL HTTPS válida."; return@setOnClickListener }
   getPreferences(0).edit().putString("endpoint",base).apply()
   output.text="Consultando..."
   Thread {
    val message=try {
     val connection=(URL(base).openConnection() as HttpURLConnection).apply { connectTimeout=8000; readTimeout=8000; requestMethod="GET" }
     try { if(connection.responseCode != 200) "Servidor respondeu HTTP ${connection.responseCode}" else {
      val json=JSONObject(connection.inputStream.bufferedReader().use { it.readText() })
      val account=json.optString("conta", json.optString("account", "Não informado"))
      val summary=json.toString(2).take(3500)
      "Conta: $account\n\n$summary"
     }} finally { connection.disconnect() }
    } catch(e:Exception) { "Falha ao consultar: ${e.javaClass.simpleName}" }
    runOnUiThread { output.text=message }
   }.start()
  }
 }
}
