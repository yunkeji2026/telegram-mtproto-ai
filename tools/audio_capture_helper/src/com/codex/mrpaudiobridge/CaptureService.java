package com.codex.mrpaudiobridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioPlaybackCaptureConfiguration;
import android.media.AudioRecord;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

public class CaptureService extends Service {
    public static final String CHANNEL_ID = "mrp_audio_bridge";
    private volatile boolean running = false;
    private Thread worker;
    private MediaProjection projection;
    private AudioRecord recorder;
    private File outDir;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        ensureChannel();
        startForeground(7, notification("Recording Messenger playback"));

        int resultCode = intent.getIntExtra("result_code", 0);
        Intent resultData = intent.getParcelableExtra("result_data");
        int durationMs = intent.getIntExtra("duration_ms", 6000);
        MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        projection = mpm.getMediaProjection(resultCode, resultData);
        running = true;
        worker = new Thread(() -> record(durationMs), "mrp-audio-capture");
        worker.start();
        return START_NOT_STICKY;
    }

    private void record(int durationMs) {
        outDir = getExternalFilesDir(Environment.DIRECTORY_MUSIC);
        if (outDir != null) outDir.mkdirs();
        status("record_start duration_ms=" + durationMs);
        if (outDir == null) {
            error("external_files_dir_null");
            cleanup();
            stopForeground(true);
            stopSelf();
            return;
        }
        File out = new File(outDir, "mrpa_capture.wav");
        int sampleRate = 16000;
        int channelMask = AudioFormat.CHANNEL_IN_MONO;
        int encoding = AudioFormat.ENCODING_PCM_16BIT;
        int minBuffer = AudioRecord.getMinBufferSize(sampleRate, channelMask, encoding);
        int bufferSize = Math.max(minBuffer, sampleRate);

        try {
            if (projection == null) {
                throw new IllegalStateException("MediaProjection is null");
            }
            AudioPlaybackCaptureConfiguration config =
                    new AudioPlaybackCaptureConfiguration.Builder(projection)
                            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                            .addMatchingUsage(AudioAttributes.USAGE_GAME)
                            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                            .build();
            AudioFormat format = new AudioFormat.Builder()
                    .setSampleRate(sampleRate)
                    .setEncoding(encoding)
                    .setChannelMask(channelMask)
                    .build();
            recorder = new AudioRecord.Builder()
                    .setAudioFormat(format)
                    .setBufferSizeInBytes(bufferSize)
                    .setAudioPlaybackCaptureConfig(config)
                    .build();
            status("audio_record_state=" + recorder.getState() + " buffer=" + bufferSize);

            try (FileOutputStream fos = new FileOutputStream(out)) {
                writeWavHeader(fos, sampleRate, 1, 16, 0);
                byte[] buf = new byte[bufferSize];
                long bytes = 0;
                long endAt = System.currentTimeMillis() + Math.max(1000, durationMs);
                recorder.startRecording();
                status("recording_state=" + recorder.getRecordingState());
                while (running && System.currentTimeMillis() < endAt) {
                    int n = recorder.read(buf, 0, buf.length);
                    if (n > 0) {
                        fos.write(buf, 0, n);
                        bytes += n;
                    }
                }
                fos.flush();
                patchWavHeader(out, bytes);
                status("record_done bytes=" + bytes);
            }
        } catch (Throwable t) {
            error(t.getClass().getName() + ": " + t.getMessage());
        } finally {
            cleanup();
            stopForeground(true);
            stopSelf();
        }
    }

    private void status(String message) {
        writeText("mrpa_capture_status.txt", System.currentTimeMillis() + " " + message + "\n", true);
    }

    private void error(String message) {
        writeText("mrpa_capture_error.txt", message == null ? "" : message, false);
        status("error=" + message);
    }

    private void writeText(String name, String text, boolean append) {
        try {
            File dir = outDir != null ? outDir : getExternalFilesDir(Environment.DIRECTORY_MUSIC);
            if (dir == null) return;
            dir.mkdirs();
            File file = new File(dir, name);
            try (FileOutputStream fos = new FileOutputStream(file, append)) {
                fos.write((text == null ? "" : text).getBytes("UTF-8"));
            }
        } catch (Exception ignored) {
        }
    }

    private void cleanup() {
        running = false;
        try {
            if (recorder != null) {
                recorder.stop();
                recorder.release();
            }
        } catch (Exception ignored) {
        }
        try {
            if (projection != null) projection.stop();
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onDestroy() {
        cleanup();
        super.onDestroy();
    }

    private void ensureChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "MRP Audio Bridge", NotificationManager.IMPORTANCE_LOW);
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            nm.createNotificationChannel(ch);
        }
    }

    private Notification notification(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        return b.setContentTitle("MRP Audio Bridge")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setOngoing(true)
                .build();
    }

    private static void writeWavHeader(FileOutputStream fos, int sampleRate, int channels, int bits, long dataLen) throws IOException {
        ByteBuffer b = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        b.put(new byte[]{'R','I','F','F'});
        b.putInt((int) (36 + dataLen));
        b.put(new byte[]{'W','A','V','E','f','m','t',' '});
        b.putInt(16);
        b.putShort((short) 1);
        b.putShort((short) channels);
        b.putInt(sampleRate);
        b.putInt(sampleRate * channels * bits / 8);
        b.putShort((short) (channels * bits / 8));
        b.putShort((short) bits);
        b.put(new byte[]{'d','a','t','a'});
        b.putInt((int) dataLen);
        fos.write(b.array());
    }

    private static void patchWavHeader(File file, long dataLen) throws IOException {
        try (RandomAccessFile raf = new RandomAccessFile(file, "rw")) {
            raf.seek(4);
            raf.write(intLE((int) (36 + dataLen)));
            raf.seek(40);
            raf.write(intLE((int) dataLen));
        }
    }

    private static byte[] intLE(int v) {
        return new byte[]{(byte) v, (byte) (v >> 8), (byte) (v >> 16), (byte) (v >> 24)};
    }
}
