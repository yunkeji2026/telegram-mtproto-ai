package com.codex.mrpaudiobridge;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.media.projection.MediaProjectionManager;
import android.os.Bundle;
import android.os.Environment;
import android.widget.TextView;

import java.io.File;
import java.io.FileOutputStream;

public class MainActivity extends Activity {
    private static final int REQ_CAPTURE = 4101;
    private int durationMs = 6000;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        TextView tv = new TextView(this);
        tv.setText("MRP Audio Bridge\nRequesting playback capture permission...");
        tv.setTextSize(18);
        tv.setPadding(36, 80, 36, 36);
        setContentView(tv);
        durationMs = getIntent().getIntExtra("duration_ms", 6000);
        status("activity_start duration_ms=" + durationMs);
        MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ_CAPTURE);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_CAPTURE) {
            status("unexpected_result request_code=" + requestCode);
            finish();
            return;
        }
        if (resultCode != RESULT_OK || data == null) {
            status("capture_denied result_code=" + resultCode + " data_null=" + (data == null));
            finish();
            return;
        }
        Intent svc = new Intent(this, CaptureService.class);
        svc.putExtra("result_code", resultCode);
        svc.putExtra("result_data", data);
        svc.putExtra("duration_ms", durationMs);
        try {
            startForegroundService(svc);
            status("service_start_requested");
        } catch (Throwable t) {
            status("service_start_error " + t.getClass().getName() + ": " + t.getMessage());
        }
        finish();
    }

    private void status(String message) {
        try {
            File dir = getExternalFilesDir(Environment.DIRECTORY_MUSIC);
            if (dir == null) return;
            dir.mkdirs();
            File file = new File(dir, "mrpa_capture_status.txt");
            try (FileOutputStream fos = new FileOutputStream(file, true)) {
                fos.write((System.currentTimeMillis() + " " + message + "\n").getBytes("UTF-8"));
            }
        } catch (Exception ignored) {
        }
    }
}
