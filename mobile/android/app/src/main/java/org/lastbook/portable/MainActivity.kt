package org.lastbook.portable

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.storage.StorageManager
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import kotlin.concurrent.thread

/**
 * Lastbook for Android: one screen. First it asks which library to open (a LastbookData folder on a plugged-in drive, a
 * memory card or the phone itself); then it is a WebView on the pages the desktop app shows, served by the desktop's own
 * engine running inside this app (lastbook_android.py), at 127.0.0.1.
 *
 * The WebView goes nowhere but 127.0.0.1. A link to anywhere else does nothing, as in the desktop app.
 */
class MainActivity : AppCompatActivity() {
    private lateinit var web: WebView
    private lateinit var chooser: LinearLayout
    private lateinit var frame: FrameLayout
    private var port = 0
    private var fullscreen: View? = null
    private var fullscreenDone: WebChromeClient.CustomViewCallback? = null

    private val prefs by lazy { getSharedPreferences("lastbook", Context.MODE_PRIVATE) }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        frame = FrameLayout(this)
        web = WebView(this)
        web.visibility = View.GONE
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.allowFileAccess = false
        web.settings.allowContentAccess = false
        web.settings.setSupportMultipleWindows(false)
        web.settings.mediaPlaybackRequiresUserGesture = true
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                // Only the app's own pages. Anything else is a web address, and this app opens none.
                val u = request.url
                return !(u.scheme == "http" && u.host == "127.0.0.1" && u.port == port)
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {   // a video asked to fill the screen
                fullscreen?.let { frame.removeView(it) }
                fullscreen = view
                fullscreenDone = callback
                frame.addView(view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                web.visibility = View.GONE
            }

            override fun onHideCustomView() {
                fullscreen?.let { frame.removeView(it) }
                fullscreen = null
                fullscreenDone = null
                web.visibility = View.VISIBLE
            }
        }
        // The pages offer setup and the builder to a browser on the computer the library is open on. Here there is neither:
        // the page is told so before any of its own code runs (web/app.js reads it).
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            WebViewCompat.addDocumentStartJavaScript(web, "window.LB_NO_SETUP = true;", setOf("http://127.0.0.1"))
        }
        frame.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        chooser = LinearLayout(this)
        chooser.orientation = LinearLayout.VERTICAL
        val pad = dp(20)
        chooser.setPadding(pad, pad, pad, pad)
        val scroll = ScrollView(this)
        scroll.addView(chooser)
        frame.addView(scroll, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(frame)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    fullscreen != null -> fullscreenDone?.onCustomViewHidden()
                    web.visibility == View.VISIBLE && web.canGoBack() -> web.goBack()
                    else -> { isEnabled = false; onBackPressedDispatcher.onBackPressed() }
                }
            }
        })
    }

    override fun onResume() {
        super.onResume()
        if (port == 0) choose()     // back from Settings, or a drive plugged in meanwhile: look again
    }

    private fun dp(n: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, n.toFloat(), resources.displayMetrics).toInt()

    private fun say(text: String, size: Float = 16f, bold: Boolean = false): TextView {
        val t = TextView(this)
        t.text = text
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
        if (bold) t.setTypeface(t.typeface, android.graphics.Typeface.BOLD)
        t.setPadding(0, dp(6), 0, dp(6))
        chooser.addView(t)
        return t
    }

    private fun button(text: String, then: () -> Unit) {
        val b = Button(this)
        b.text = text
        b.isAllCaps = false
        b.gravity = Gravity.START or Gravity.CENTER_VERTICAL
        b.setOnClickListener { then() }
        val lp = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        lp.topMargin = dp(8)
        chooser.addView(b, lp)
    }

    /** Where a library could be: every storage volume the system has mounted (the phone's own, a memory card, a plugged-in drive). */
    private fun storageRoots(): List<String> {
        val out = LinkedHashSet<String>()
        val sm = getSystemService(Context.STORAGE_SERVICE) as StorageManager
        for (v in sm.storageVolumes) v.directory?.let { out.add(it.absolutePath) }
        File("/storage").listFiles()?.forEach { f ->        // a drive the list above leaves out on some phones
            if (f.isDirectory && f.name != "self" && f.name != "emulated" && f.canRead()) out.add(f.absolutePath)
        }
        return out.toList()
    }

    private fun choose() {
        chooser.removeAllViews()
        say("Lastbook", 26f, true)
        if (!Environment.isExternalStorageManager()) {
            say("Lastbook opens a library that is ordinary files in a folder called LastbookData: on a drive plugged into this device, on a memory card, or on the device itself. To read it where it is, Android has to be told this app may read files.")
            say("Nothing is copied, and nothing is sent anywhere: this app has no way to reach the internet's sites.")
            button("Allow Lastbook to read files…") {
                try {
                    startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName")))
                } catch (e: Exception) {
                    startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
                }
            }
            return
        }
        say("Looking for a library…")
        thread {
            val py = python()
            val found = try {
                py.getModule("lastbook_android").callAttr("libraries", storageRoots().toTypedArray()).asList().map { it.asList().map { s -> s.toString() } }
            } catch (e: Exception) {
                emptyList()
            }
            val about = try { py.getModule("lastbook_android").callAttr("python_says").toString() } catch (e: Exception) { e.toString() }
            runOnUiThread {
                chooser.removeAllViews()
                say("Lastbook", 26f, true)
                val last = prefs.getString("library", null)
                if (found.isEmpty()) {
                    say("No library was found.", 18f, true)
                    say("A library is a folder called LastbookData, made on a computer with Lastbook (Build your own library). Plug in the drive it is on, or copy the folder to this device, then look again.")
                } else {
                    say(if (found.size == 1) "One library found. Open it:" else "${found.size} libraries found. Choose one:")
                    for (lib in found) {
                        val where = lib[0].removeSuffix("/LastbookData")
                        button((if (lib[0] == last) "Open again: " else "") + where + (if (lib[1].isNotEmpty()) "\n" + lib[1] else "")) { open(lib[0]) }
                    }
                }
                button("Look again") { choose() }
                say(about, 12f).setTextColor(Color.GRAY)
            }
        }
    }

    private fun python(): Python {
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        return Python.getInstance()
    }

    /** The pages (web/ and content/) out of the package into the app's own folder, once for each version of the app. */
    private fun pages(): File {
        val version = packageManager.getPackageInfo(packageName, 0).longVersionCode
        val dir = File(filesDir, "pages-$version")
        val done = File(dir, ".unpacked")
        if (done.exists()) return dir
        filesDir.listFiles()?.filter { it.name.startsWith("pages-") }?.forEach { it.deleteRecursively() }   // an older version's
        copyAsset("lastbook", dir)
        done.writeText("ok")
        return dir
    }

    private fun copyAsset(path: String, to: File) {
        val names = assets.list(path) ?: emptyArray()
        if (names.isEmpty()) {      // a file
            to.parentFile?.mkdirs()
            assets.open(path).use { src -> to.outputStream().use { dst -> src.copyTo(dst) } }
        } else {
            to.mkdirs()
            for (n in names) copyAsset("$path/$n", File(to, n))
        }
    }

    private fun open(library: String) {
        chooser.removeAllViews()
        say("Lastbook", 26f, true)
        say("Opening the library…")
        thread {
            try {
                val state = File(filesDir, "state").apply { mkdirs() }
                val p = python().getModule("lastbook_android").callAttr("start", library, pages().absolutePath, state.absolutePath, cacheDir.absolutePath).toInt()
                prefs.edit().putString("library", library).apply()
                runOnUiThread {
                    port = p
                    (chooser.parent as View).visibility = View.GONE
                    web.visibility = View.VISIBLE
                    web.loadUrl("http://127.0.0.1:$p/")
                }
            } catch (e: Exception) {
                val why = (e.message ?: e.toString()).lines().lastOrNull { it.isNotBlank() } ?: "It could not be opened."
                runOnUiThread {
                    chooser.removeAllViews()
                    say("Lastbook", 26f, true)
                    say("The library did not open.", 18f, true)
                    say(why)
                    button("Choose again") { choose() }
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        if (isFinishing && Python.isStarted()) {
            try { Python.getInstance().getModule("lastbook_android").callAttr("stop") } catch (e: Exception) { }
        }
    }
}
