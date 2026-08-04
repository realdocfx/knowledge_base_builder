package org.kbb.portal

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * A thin WebView shell hosting the KBB portal in a dedicated, chromeless window --
 * the Android analog of the Tauri desktop launcher. The portal itself (FastAPI +
 * kiwix) runs in Termux (see ../ANDROID.md); this activity waits for it to answer on
 * 127.0.0.1:8080, then loads it full-screen. There is no native KBB code here.
 */
class MainActivity : AppCompatActivity() {

    private val portalUrl = "http://127.0.0.1:8080/"
    private var webView: WebView? = null
    private lateinit var status: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        status = TextView(this).apply {
            text = getString(R.string.starting)
            textSize = 16f
            setPadding(64, 128, 64, 64)
        }
        setContentView(status)

        // Back button navigates WebView history, then leaves the app.
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

        waitForPortalThenLoad()
    }

    private fun waitForPortalThenLoad() {
        thread {
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
        wv.webViewClient = WebViewClient()   // keep all navigation inside the shell
        setContentView(wv)
        wv.loadUrl(portalUrl)
        webView = wv
    }
}
