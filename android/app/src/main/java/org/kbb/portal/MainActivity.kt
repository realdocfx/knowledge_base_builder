package org.kbb.portal

import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * A thin WebView shell hosting the KBB portal in a dedicated, chromeless window --
 * the Android analog of the Tauri launcher. The portal (FastAPI + kiwix) runs in
 * Termux; this activity can START it there autonomously via Termux's RUN_COMMAND
 * intent (Termux needs `allow-external-apps=true`, a one-time flag), so no manual
 * terminal work is needed. If that path is unavailable it offers a one-tap "Copy
 * start command" + "Open Termux". Then it waits for 127.0.0.1:8080 and loads it.
 */
class MainActivity : AppCompatActivity() {

    private val portalUrl = "http://127.0.0.1:8080/"
    private val contentDir = "/storage/emulated/0/kbb-content"
    private val startScript = "/storage/emulated/0/kbb-android/kbb-start.sh"
    private val startCommand = "bash ~/storage/shared/kbb-android/kbb-start.sh ~/storage/shared/kbb-content"

    private var webView: WebView? = null
    private lateinit var status: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildLoadingView())

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                val wv = webView
                if (wv != null && wv.canGoBack()) {
                    wv.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        thread {
            // Start the backend only if it is not already serving (avoid two portals
            // racing for the port).
            if (!probe()) tryStartBackendViaTermux()
            var ok = false
            var tries = 0
            while (!ok && tries < 240) {          // up to ~120 s
                ok = probe()
                if (!ok) Thread.sleep(500)
                tries++
            }
            runOnUiThread {
                if (ok) showWebView() else status.text = getString(R.string.not_reachable)
            }
        }
    }

    private fun buildLoadingView(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(64, 160, 64, 64)
        }
        status = TextView(this).apply {
            text = getString(R.string.starting)
            textSize = 16f
        }
        val copyBtn = Button(this).apply {
            text = getString(R.string.copy_cmd)
            setOnClickListener {
                val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("kbb-start", startCommand))
                Toast.makeText(this@MainActivity, R.string.copied, Toast.LENGTH_SHORT).show()
            }
        }
        val termuxBtn = Button(this).apply {
            text = getString(R.string.open_termux)
            setOnClickListener { openTermux() }
        }
        root.addView(status)
        root.addView(copyBtn)
        root.addView(termuxBtn)
        return root
    }

    /** Ask Termux to run kbb-start.sh (background). Silently degrades if Termux is
     *  absent or allow-external-apps is off -- the buttons above are the fallback. */
    private fun tryStartBackendViaTermux() {
        try {
            val i = Intent().apply {
                setClassName("com.termux", "com.termux.app.RunCommandService")
                action = "com.termux.RUN_COMMAND"
                putExtra("com.termux.RUN_COMMAND_PATH",
                    "/data/data/com.termux/files/usr/bin/bash")
                putExtra("com.termux.RUN_COMMAND_ARGUMENTS", arrayOf(startScript, contentDir))
                putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(i)
            else startService(i)
        } catch (e: Exception) {
            // Termux not installed, or RUN_COMMAND not permitted -> user uses the buttons.
        }
    }

    private fun openTermux() {
        val i = packageManager.getLaunchIntentForPackage("com.termux")
        if (i != null) startActivity(i)
        else Toast.makeText(this, R.string.no_termux, Toast.LENGTH_LONG).show()
    }

    private fun probe(): Boolean = try {
        val c = URL(portalUrl).openConnection() as HttpURLConnection
        c.connectTimeout = 1500
        c.readTimeout = 1500
        c.requestMethod = "GET"
        val code = c.responseCode
        c.disconnect()
        code in 200..399
    } catch (e: Exception) {
        false
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun showWebView() {
        val wv = WebView(this)
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mediaPlaybackRequiresUserGesture = false
            useWideViewPort = true
            loadWithOverviewMode = true
        }
        wv.webViewClient = WebViewClient()
        setContentView(wv)
        wv.loadUrl(portalUrl)
        webView = wv
    }
}
