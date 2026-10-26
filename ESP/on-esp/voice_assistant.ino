/*
  ESP32 voice assistant: "Hey Kate" wake word -> record -> server -> audio reply

  Mic:      MAX4466 analog out -> GPIO32 (ADC1_CHANNEL_4)
  Speaker:  MAX98357A (optional, set HAVE_SPEAKER 1 once it is wired)
              BCLK -> GPIO26, LRC -> GPIO25, DIN -> GPIO22, VIN -> 5V, GND -> GND

  Files in this sketch folder (they open as tabs in the Arduino IDE):
    voice_assistant.ino   this file
    hey_kate_model.h      your hey_kate.tflite as a C array (62,304 bytes, stays in flash)

  Needs (read the notes at the bottom of the chat message too):
    * Arduino-ESP32 core 2.0.x (I2S built-in ADC mode is gone in 3.x)
    * A TensorFlow Lite Micro library that is recent enough for this model
      (CALL_ONCE / VAR_HANDLE / READ_VARIABLE / ASSIGN_VARIABLE ops) AND contains the
      microfrontend (tensorflow/lite/experimental/microfrontend/lib/frontend.h)
*/

#include <WiFi.h>
#include <Preferences.h>
#include <driver/i2s.h>
#include <driver/adc.h>

// ---- TensorFlow Lite Micro (adjust these include paths to match your library) ----
#include <TensorFlowLite.h>   // library main header first (use <TensorFlowLite.h> for the pschatzmann library)
#include <tensorflow/lite/micro/micro_interpreter.h>
#include <tensorflow/lite/micro/micro_mutable_op_resolver.h>
#include <tensorflow/lite/micro/micro_allocator.h>
#include <tensorflow/lite/micro/micro_resource_variable.h>
#include <tensorflow/lite/schema/schema_generated.h>
#include <tensorflow/lite/experimental/microfrontend/lib/frontend.h>
#include <tensorflow/lite/experimental/microfrontend/lib/frontend_util.h>

#include "hey_kate_model.h"

// ===================== CONFIG =====================
const char*    WIFI_SSID   = "";                  // <- change to your Wi-Fi SSID
const char*    WIFI_PASS   = "";                  // <- change to your Wi-Fi password
const char*    SERVER_HOST = "";                  // <- change to your server's IP
const uint16_t SERVER_PORT = 8000;
const char*    SERVER_PATH = "/talk";
#define HAVE_SPEAKER 0                          // set to 1 when the MAX98357A is wired

// Wake word detection. Check these against the .json that came with hey_kate.tflite
// (probability_cutoff and sliding_window_size). These are common defaults.
const float WAKE_CUTOFF        = 0.97f;
const int   WAKE_WINDOW        = 5;             // number of inferences averaged
const int   WAKE_COOLDOWN_MS   = 1500;

// Recording after the wake word
const int   NO_SPEECH_MS       = 4000;          // give up if you never speak
const int   SILENCE_MS         = 450;           // stop quickly after the user finishes
const int   MAX_RECORD_MS      = 10000;

// Announcements (timers, reminders, scheduled news...) are PUSHED by the server: the ESP keeps one request
// open and the server answers it the moment something has to be said. If nothing happens, the server
// answers with an empty "I am alive" reply after SERVER_CHECK_MS and the ESP opens a new request, so the
// server is checked every 5 minutes. If the server cannot be reached, the ESP retries every RETRY_MS (30 s).
const bool          WAIT_FOR_ANNOUNCEMENTS = true;
const unsigned long SERVER_CHECK_MS        = 5UL * 60UL * 1000UL;
const unsigned long RETRY_MS               = 30000UL;
const float SPEECH_MIN_RMS     = 100.0f;        // 16-bit units, see Serial debug output
const float SPEECH_RATIO       = 3.0f;          // speech = noise floor * this
const float END_LEVEL_RATIO    = 0.15f;         // after speech, silence = below this fraction of your speaking level

const bool  DEBUG_LEVELS       = true;

// Mic tuning
const float MIC_GAIN           = 36.0f;         // digital gain. Too quiet: raise it. Distorted: lower it
const float LIMIT_KNEE         = 18000.0f;      // above this the signal is compressed softly instead of hard-clipping
#define     DEGLITCH           1                // 1 = median-of-5 filter on the raw ADC samples (removes single bad samples)
#define     DMA_LEN            512              // I2S ADC DMA buffer, in samples (max 1024). 1024 gave a click every 16 ms; 512 -> 8 ms.
const wifi_power_t WIFI_TX_POWER = WIFI_POWER_15dBm;   // lower TX bursts disturb the ADC less (WIFI_POWER_19_5dBm = default)

int volume = 40;                                // 0..100 (software volume, squared curve). The server overrides it on every reply ("Hey Kate, louder"); v50 in the Serial Monitor only lasts until the next reply
// ==================================================

const int SAMPLE_RATE = 16000;
const int OVERSAMPLE  = 4;                     // ADC runs at 4 x 16 kHz, groups of 4 are averaged (halves the noise)
const int CHUNK       = 160;                    // 10 ms of audio per read
const int SEND_BATCH  = 4;                      // chunks per HTTP chunk (40 ms)
const int TENSOR_ARENA_SIZE = 48 * 1024;        // trim after reading arena_used_bytes()

// ---------- speaker ----------
int16_t applyVolume(int16_t s) {
  int32_t gain = (int32_t)volume * volume;      // 0..10000
  return (int16_t)(((int32_t)s * gain) / 10000);
}

void setupSpeaker() {
#if HAVE_SPEAKER
  i2s_config_t c = {};
  c.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  c.sample_rate          = 24000;
  c.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  c.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT;
  c.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  c.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  c.dma_buf_count        = 8;
  c.dma_buf_len          = 256;
  c.tx_desc_auto_clear   = true;
  i2s_driver_install(I2S_NUM_1, &c, 0, NULL);
  i2s_pin_config_t pins = {};
  pins.bck_io_num   = 26;
  pins.ws_io_num    = 25;
  pins.data_out_num = 22;
  pins.data_in_num  = I2S_PIN_NO_CHANGE;
  i2s_set_pin(I2S_NUM_1, &pins);
#endif
}

static uint32_t playedSamples = 0;
static uint32_t replyRate = 24000;          // the server sends Kokoro's native 24 kHz
void setSpeakerRate(uint32_t rate) {
  replyRate = rate;
#if HAVE_SPEAKER
  i2s_set_sample_rates(I2S_NUM_1, rate);
#endif
}
void playChunk(int16_t* s, size_t n) {
  for (size_t i = 0; i < n; i++) s[i] = applyVolume(s[i]);
  playedSamples += n;
#if HAVE_SPEAKER
  size_t written = 0;
  i2s_write(I2S_NUM_1, s, n * 2, &written, portMAX_DELAY);
#endif
}

// ---------- Wi-Fi with fast reconnect ----------
void connectWiFi() {
  Preferences p;
  p.begin("wifi", false);
  uint8_t bssid[6];
  bool have = p.getBytes("bssid", bssid, 6) == 6;
  int  ch   = p.getInt("ch", 0);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                       // modem sleep wakes the radio every beacon (102.4 ms) and that shows up in the audio
  WiFi.setTxPower(WIFI_TX_POWER);
  unsigned long t0 = millis();
  if (have && ch > 0) {
    WiFi.begin(WIFI_SSID, WIFI_PASS, ch, bssid, true);   // skips the scan
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 4000) delay(50);
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.disconnect();
      have = false;
    }
  }
  if (!have || WiFi.status() != WL_CONNECTED) {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(100);
  }
  if (WiFi.status() == WL_CONNECTED) {
    p.putBytes("bssid", WiFi.BSSID(), 6);
    p.putInt("ch", WiFi.channel());
    Serial.printf("WiFi OK, IP %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("WiFi FAILED");
  }
  p.end();
}

// ---------- mic (I2S built-in ADC on GPIO32) ----------
static bool extPrimed = false;               // deglitch history needs (re)priming after the mic driver restarts
void setupMic(int rate) {
  extPrimed = false;
  i2s_config_t cfg = {};
  cfg.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_ADC_BUILT_IN);
  cfg.sample_rate          = rate;
  cfg.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count        = 8;
  cfg.dma_buf_len          = DMA_LEN;
  cfg.use_apll             = false;
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  adc1_config_width(ADC_WIDTH_BIT_12);
  adc1_config_channel_atten(ADC1_CHANNEL_4, ADC_ATTEN_DB_11);   // GPIO32
  i2s_set_adc_mode(ADC_UNIT_1, ADC1_CHANNEL_4);
  i2s_adc_enable(I2S_NUM_0);
}

static uint16_t rawBuf[CHUNK * OVERSAMPLE];
static uint16_t extBuf[CHUNK * OVERSAMPLE + 4];   // 4 samples carried over from the previous chunk + the new chunk

// median of 5 (sorting network, 7 compare-swaps)
static inline uint16_t median5(uint16_t a, uint16_t b, uint16_t c, uint16_t d, uint16_t e) {
  #define SWP(x, y) if (x > y) { uint16_t t = x; x = y; y = t; }
  SWP(a, b) SWP(d, e) SWP(a, d) SWP(b, e) SWP(c, d) SWP(b, c) SWP(c, d)
  #undef SWP
  return c;
}
static int16_t  pcm[CHUNK];
static float    dc = 2048.0f;                // running DC offset (raw ADC units)
static float    noiseFloor = 100.0f;

// Reads 10 ms: OVERSAMPLE raw ADC samples are averaged into each output sample (a cheap
// low-pass filter that cuts the white noise by about half), DC removed, scaled to 16-bit. Returns rms.
float readChunk() {
  size_t total = 0, got = 0;
  while (total < sizeof(rawBuf)) {
    i2s_read(I2S_NUM_0, ((uint8_t*)rawBuf) + total, sizeof(rawBuf) - total, &got, portMAX_DELAY);
    total += got;
  }
  const int N = CHUNK * OVERSAMPLE;
  for (int i = 0; i < N; i++) rawBuf[i] &= 0x0FFF;
#if DEGLITCH
  if (!extPrimed) { for (int i = 0; i < 4; i++) extBuf[i] = rawBuf[0]; extPrimed = true; }
  memcpy(extBuf + 4, rawBuf, N * sizeof(uint16_t));
  for (int i = 0; i < N; i++)                                 // window i..i+4 is centred on extBuf[i+2]
    rawBuf[i] = median5(extBuf[i], extBuf[i + 1], extBuf[i + 2], extBuf[i + 3], extBuf[i + 4]);
  memcpy(extBuf, extBuf + N, 4 * sizeof(uint16_t));           // keep the last 4 for the next chunk
#endif
  float sumsq = 0;
  for (int i = 0; i < CHUNK; i++) {
    int sum = 0;
    for (int k = 0; k < OVERSAMPLE; k++) sum += rawBuf[i * OVERSAMPLE + k];
    float v = (float)sum / OVERSAMPLE;
    dc += (v - dc) * 0.001f;
    float s = (v - dc) * MIC_GAIN;
    float as = fabsf(s);                          // soft limiter: loud peaks are squashed, not chopped off
    if (as > LIMIT_KNEE) {
      as = LIMIT_KNEE + (as - LIMIT_KNEE) * 0.25f;
      if (as > 32767) as = 32767;
      s = (s < 0) ? -as : as;
    }
    pcm[i] = (int16_t)s;
    sumsq += s * s;
  }
  return sqrtf(sumsq / CHUNK);
}

void stopMic() {
  i2s_adc_disable(I2S_NUM_0);
  i2s_driver_uninstall(I2S_NUM_0);
}

// The I2S ADC does not run at the rate you ask for. Measure the real rate and
// correct the requested rate until the real one is ~16 kHz.
static int i2sRate = 12000;                    // first guess; tuneMicRate() corrects it
float tuneMicRate() {
  const float target = (float)SAMPLE_RATE * OVERSAMPLE;
  float real = target;
  for (int attempt = 0; attempt < 4; attempt++) {
    setupMic(i2sRate);
    for (int i = 0; i < 20; i++) readChunk();            // let it settle, discarded
    const int n = 200;                                   // 32000 samples
    unsigned long ts = millis();
    for (int i = 0; i < n; i++) readChunk();
    real = (float)n * CHUNK * OVERSAMPLE * 1000.0f / (float)(millis() - ts);
    Serial.printf("I2S asked %d Hz -> real %.0f Hz\n", i2sRate, real);
    if (fabsf(real - target) < target * 0.05f) return real;   // within 5%, good
    stopMic();
    i2sRate = constrain((int)((float)i2sRate * target / real), 1000, 200000);
  }
  setupMic(i2sRate);                                     // use the last try anyway
  return real;
}

// ---------- wake word (microWakeWord streaming model) ----------
static struct FrontendState feState;
static uint8_t* tensorArena = nullptr;
static tflite::MicroInterpreter* interp = nullptr;
static TfLiteTensor* inTensor  = nullptr;
static TfLiteTensor* outTensor = nullptr;

static int8_t  featFrames[3][40];             // model input is [1, 3, 40]
static int     featCount = 0;
static uint8_t probWin[16];
static int     probIdx = 0, probFilled = 0;
static uint8_t lastProb = 0;

bool setupWakeWord() {
  // Frontend settings used by microWakeWord (30 ms window, 10 ms step, 40 mel channels)
  struct FrontendConfig cfg;
  FrontendFillConfigWithDefaults(&cfg);
  cfg.window.size_ms = 30;
  cfg.window.step_size_ms = 10;
  cfg.filterbank.num_channels = 40;
  cfg.filterbank.lower_band_limit = 125.0f;
  cfg.filterbank.upper_band_limit = 7500.0f;
  cfg.noise_reduction.smoothing_bits = 10;
  cfg.noise_reduction.even_smoothing = 0.025f;
  cfg.noise_reduction.odd_smoothing = 0.06f;
  cfg.noise_reduction.min_signal_remaining = 0.05f;
  cfg.pcan_gain_control.enable_pcan = 1;
  cfg.pcan_gain_control.strength = 0.95f;
  cfg.pcan_gain_control.offset = 80.0f;
  cfg.pcan_gain_control.gain_bits = 21;
  cfg.log_scale.enable_log = 1;
  cfg.log_scale.scale_shift = 6;
  if (!FrontendPopulateState(&cfg, &feState, SAMPLE_RATE)) {
    Serial.println("Frontend init failed");
    return false;
  }

  const tflite::Model* model = tflite::GetModel(hey_kate_model);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    Serial.println("Model schema version mismatch");
    return false;
  }

  tensorArena = (uint8_t*)heap_caps_malloc(TENSOR_ARENA_SIZE, MALLOC_CAP_8BIT);
  if (!tensorArena) { Serial.println("No RAM for tensor arena"); return false; }

  static tflite::MicroMutableOpResolver<16> resolver;
  resolver.AddCallOnce();
  resolver.AddVarHandle();
  resolver.AddReshape();
  resolver.AddReadVariable();
  resolver.AddStridedSlice();
  resolver.AddConcatenation();
  resolver.AddAssignVariable();
  resolver.AddConv2D();
  resolver.AddDepthwiseConv2D();
  resolver.AddSplitV();
  resolver.AddFullyConnected();
  resolver.AddLogistic();
  resolver.AddQuantize();

  tflite::MicroAllocator* alloc = tflite::MicroAllocator::Create(tensorArena, TENSOR_ARENA_SIZE);
  tflite::MicroResourceVariables* vars = tflite::MicroResourceVariables::Create(alloc, 20);
  static tflite::MicroInterpreter staticInterp(model, resolver, alloc, vars);
  interp = &staticInterp;
  if (interp->AllocateTensors() != kTfLiteOk) {
    Serial.println("AllocateTensors failed (arena too small?)");
    return false;
  }
  inTensor  = interp->input(0);
  outTensor = interp->output(0);
  Serial.printf("Wake model ready. Arena used: %u bytes. Input type %d, output type %d\n",
                (unsigned)interp->arena_used_bytes(), inTensor->type, outTensor->type);
  return true;
}

// Feeds audio to the frontend; returns true when "Hey Kate" is detected
bool feedWakeWord(const int16_t* s, size_t n) {
  bool detected = false;
  while (n > 0) {
    size_t read = 0;
    struct FrontendOutput out = FrontendProcessSamples(&feState, s, n, &read);
    if (read == 0) break;
    s += read;
    n -= read;
    if (out.size == 0) continue;

    // microfrontend output -> int8 model input (same scaling microWakeWord uses)
    for (int i = 0; i < 40; i++) {
      int32_t v = ((int32_t)out.values[i] * 256 + 333) / 666 - 128;
      if (v < -128) v = -128;
      if (v > 127) v = 127;
      featFrames[featCount][i] = (int8_t)v;
    }
    if (++featCount < 3) continue;
    featCount = 0;

    memcpy(inTensor->data.int8, featFrames, sizeof(featFrames));
    if (interp->Invoke() != kTfLiteOk) { Serial.println("Invoke failed"); continue; }
    lastProb = outTensor->data.uint8[0];

    probWin[probIdx] = lastProb;
    probIdx = (probIdx + 1) % WAKE_WINDOW;
    if (probFilled < WAKE_WINDOW) probFilled++;
    if (probFilled == WAKE_WINDOW) {
      int sum = 0;
      for (int i = 0; i < WAKE_WINDOW; i++) sum += probWin[i];
      if ((float)sum / WAKE_WINDOW >= WAKE_CUTOFF * 255.0f) detected = true;
    }
  }
  return detected;
}

void resetWakeWord() {
  FrontendReset(&feState);
  featCount = 0;
  probIdx = 0;
  probFilled = 0;
  lastProb = 0;
  memset(probWin, 0, sizeof(probWin));
}

// ---------- HTTP: chunked upload, streamed WAV reply ----------
static WiFiClient client;            // upload + reply connection (opened ahead of time)
static WiFiClient pollClient;        // separate connection that waits for announcements
static bool waitOpen = false;        // true while the "tell me when something happens" request is open
static unsigned long waitOpenedAt = 0, nextAnnounceTry = 0;
static WiFiClient* cur = nullptr;    // which connection the reply readers use
static bool uploadReady = false;     // true while a pre-opened upload connection is waiting
static unsigned long uploadOpenedAt = 0, nextPrepare = 0;
const unsigned long UPLOAD_REFRESH_MS = 300000;  // renew an idle pre-opened connection this often (5 min)

void sendChunk(const uint8_t* data, size_t len) {
  client.printf("%X\r\n", (unsigned)len);
  client.write(data, len);
  client.print("\r\n");
}

// Opens the connection and sends the HTTP headers NOW, while nobody is talking, so that after
// "Hey Kate" the audio can start streaming at once instead of waiting for a connect.
bool prepareUpload(int connectTimeoutMs) {
  if (uploadReady && client.connected()) return true;
  client.stop();
  uploadReady = false;
  client.setNoDelay(true);
  if (!client.connect(SERVER_HOST, SERVER_PORT, connectTimeoutMs)) return false;
  client.printf("POST %s HTTP/1.1\r\nHost: %s\r\n"
                "Content-Type: audio/L16; rate=%d\r\n"
                "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
                SERVER_PATH, SERVER_HOST, SAMPLE_RATE);
  uploadReady = true;
  uploadOpenedAt = millis();
  return true;
}

// one byte, or -1 if nothing arrives in time / the connection is gone
int readByteTimeout(unsigned long timeoutMs) {
  unsigned long t0 = millis();
  while (!cur->available()) {
    if (!cur->connected() || millis() - t0 > timeoutMs) return -1;
    delay(1);
  }
  return cur->read();
}

// up to `want` bytes (at least 1), or -1 on timeout / closed connection
int readSome(uint8_t* buf, int want, unsigned long timeoutMs) {
  unsigned long t0 = millis();
  while (true) {
    int avail = cur->available();
    if (avail > 0) return cur->read(buf, avail < want ? avail : want);
    if (!cur->connected() || millis() - t0 > timeoutMs) return -1;
    delay(1);
  }
}

// Parsed reply headers (globals, because the Arduino IDE mangles functions that take custom struct types)
static int  hdrStatus = 0;
static bool hdrChunked = false;
static long hdrContentLength = -1;     // -1 = unknown (read until the server closes)
static bool hdrKeepGoing = false;      // X-Continue: 1 -> Kate asked a question, keep listening
static int  hdrVolume = -1;            // X-Volume: 0..100 -> the volume the user asked for (-1 = not sent)

bool readHeaders() {
  hdrStatus = 0; hdrChunked = false; hdrContentLength = -1; hdrKeepGoing = false; hdrVolume = -1;
  bool first = true;
  while (true) {
    String line;
    while (true) {
      int c = readByteTimeout(first ? 60000 : 5000);   // first byte waits for ASR + LLM
      if (c < 0) return false;
      if (c == '\n') break;
      if (c != '\r') line += (char)c;
    }
    if (first) {
      sscanf(line.c_str(), "HTTP/%*d.%*d %d", &hdrStatus);
      first = false;
      continue;
    }
    if (line.length() == 0) return true;               // blank line = end of headers
    line.toLowerCase();
    if (line.startsWith("transfer-encoding:") && line.indexOf("chunked") >= 0) hdrChunked = true;
    else if (line.startsWith("content-length:")) hdrContentLength = line.substring(15).toInt();
    else if (line.startsWith("x-continue:")) hdrKeepGoing = line.indexOf('1') >= 0;
    else if (line.startsWith("x-volume:")) hdrVolume = constrain((int)line.substring(9).toInt(), 0, 100);
  }
}

// Body reader that hides HTTP chunked encoding. Returns bytes read, 0 at the end, -1 on error.
static bool bodyChunked = false, bodyDone = false;
static long chunkLeft = 0, contentLeft = -1;

int bodyRead(uint8_t* buf, int want) {
  if (bodyDone) return 0;
  if (bodyChunked) {
    while (chunkLeft == 0) {                           // read the next chunk size line
      char line[20]; int n = 0;
      while (true) {
        int c = readByteTimeout(10000);
        if (c < 0) { bodyDone = true; return -1; }
        if (c == '\n') break;
        if (c != '\r' && n < 19) line[n++] = (char)c;
      }
      if (n == 0) continue;                            // blank line after the previous chunk
      line[n] = 0;
      chunkLeft = strtol(line, NULL, 16);
      if (chunkLeft == 0) { bodyDone = true; return 0; }
    }
    int n = readSome(buf, (want < chunkLeft) ? want : (int)chunkLeft, 10000);
    if (n < 0) { bodyDone = true; return -1; }
    chunkLeft -= n;
    return n;
  }
  if (contentLeft == 0) { bodyDone = true; return 0; }
  int n = readSome(buf, (contentLeft > 0 && contentLeft < want) ? (int)contentLeft : want, 10000);
  if (n < 0) { bodyDone = true; return 0; }            // server closed = end of body
  if (contentLeft > 0) contentLeft -= n;
  return n;
}

// Call once the request is completely sent. Plays the reply while it streams in.
// Returns true when Kate asked a question and wants you to answer without the wake word.
bool receiveAndPlayReply() {
  if (!readHeaders()) {
    Serial.println("No reply from server");
    cur->stop();
    return false;
  }
  if (hdrVolume >= 0 && hdrVolume != volume) {          // the server owns the volume (voice command), also on 204 replies
    volume = hdrVolume;
    Serial.printf("volume = %d (from server)\n", volume);
  }
  if (hdrStatus != 200) {                               // 204 = nothing to say
    Serial.printf("Server: %d (no audio)\n", hdrStatus);
    cur->stop();
    return hdrKeepGoing;
  }
  bodyChunked = hdrChunked; bodyDone = false; chunkLeft = 0; contentLeft = hdrContentLength;

  uint8_t hdr[44];
  int got = 0;
  while (got < 44) {
    int n = bodyRead(hdr + got, 44 - got);
    if (n <= 0) break;
    got += n;
  }
  if (got < 44) { cur->stop(); return hdrKeepGoing; }
  uint32_t rate = hdr[24] | (hdr[25] << 8) | (hdr[26] << 16) | ((uint32_t)hdr[27] << 24);
  if (rate < 8000 || rate > 48000) rate = 24000;
  setSpeakerRate(rate);

  static uint8_t stage[1026] __attribute__((aligned(4)));   // aligned, so int16 access is safe
  playedSamples = 0;
  uint8_t odd = 0;
  bool haveOdd = false;
  while (true) {
    int off = haveOdd ? 1 : 0;
    if (haveOdd) stage[0] = odd;
    int n = bodyRead(stage + off, 1024);
    if (n <= 0) break;
    int total = n + off;
    if (total & 1) { odd = stage[total - 1]; haveOdd = true; total--; } else haveOdd = false;
    if (total > 0) playChunk((int16_t*)stage, total / 2);
  }
  cur->stop();
  Serial.printf("Reply: %.2f s of audio at %lu Hz%s%s\n",
                playedSamples / (float)replyRate, (unsigned long)replyRate,
                HAVE_SPEAKER ? "" : "  [no speaker, nothing played]",
                hdrKeepGoing ? "  [continue]" : "");
  return hdrKeepGoing;
}

// Opens the long-poll request: the server holds it until it has something to say (or SERVER_CHECK_MS pass).
bool openAnnounceWait() {
  if (WiFi.status() != WL_CONNECTED) return false;
  pollClient.stop();
  pollClient.setNoDelay(true);
  if (!pollClient.connect(SERVER_HOST, SERVER_PORT, 300)) return false;
  pollClient.printf("GET /announce?wait=%lu HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n",
                    SERVER_CHECK_MS / 1000UL, SERVER_HOST);
  waitOpen = true;
  waitOpenedAt = millis();
  return true;
}

// Called every loop while listening. Never blocks while waiting: it only peeks at the socket, so the
// wake word keeps running. Returns true when an announcement was played.
bool serviceAnnouncements(unsigned long now) {
  if (!waitOpen) {
    if (now < nextAnnounceTry) return false;
    if (openAnnounceWait()) {
      Serial.println(">> server reachable, waiting for announcements");
    } else {
      nextAnnounceTry = millis() + RETRY_MS;
      Serial.printf(">> server not reachable, trying again in %lu s\n", RETRY_MS / 1000UL);
    }
    return false;
  }
  if (pollClient.available() > 0) {                 // the server answered: audio, or the 5-minute heartbeat (204)
    cur = &pollClient;
    uint32_t before = playedSamples;
    playedSamples = 0;
    receiveAndPlayReply();
    bool played = playedSamples > 0;
    if (!played) playedSamples = before;
    pollClient.stop();
    waitOpen = false;
    nextAnnounceTry = 0;                            // open the next request right away
    return played;
  }
  // connection gone, or the server stayed silent longer than it promised (it died without saying goodbye)
  if (!pollClient.connected() || now - waitOpenedAt > SERVER_CHECK_MS + 15000UL) {
    pollClient.stop();
    waitOpen = false;
    nextAnnounceTry = now + RETRY_MS;
    Serial.printf(">> lost the server, trying again in %lu s\n", RETRY_MS / 1000UL);
  }
  return false;
}

// Ends the idle pre-opened request cleanly (the server answers 204 at once) so a new one can be opened
void refreshUpload() {
  if (client.connected()) {
    client.print("0\r\n\r\n");
    cur = &client;
    receiveAndPlayReply();
  }
  client.stop();
  uploadReady = false;
}

// ---------- main ----------
enum State { LISTENING, RECORDING };
static State state = LISTENING;
static int16_t sendBuf[CHUNK * SEND_BATCH];
static int sendFill = 0;
static unsigned long recStart = 0, lastSpeech = 0, cooldownUntil = 0;
static bool heardSpeech = false;
static float recMaxRms = 0;
static float envRms = 0, speechLevel = 0, curThr = 0;   // smoothed level, how loud you speak, current end/start threshold

bool beginRecording(unsigned long now) {
  if (WiFi.status() != WL_CONNECTED) connectWiFi();
  if (!prepareUpload(2000)) { Serial.println("Server connect failed"); return false; }
  uploadReady = false;                            // this connection is now in use
  state = RECORDING;
  recMaxRms = 0;
  envRms = noiseFloor;
  speechLevel = 0;
  curThr = max(SPEECH_MIN_RMS, noiseFloor * SPEECH_RATIO);
  lastProb = 0;                                   // so the debug line stops showing the old 255
  Serial.println(">> connected, speak your prompt now");
  recStart = lastSpeech = now;
  heardSpeech = false;
  sendFill = 0;
  return true;
}

void afterPlayback() {
  i2s_zero_dma_buffer(I2S_NUM_0);
  resetWakeWord();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n\"Hey Kate\" assistant starting");
  tuneMicRate();                // measures the real ADC rate and corrects it to 16 kHz
  setupSpeaker();
  if (!setupWakeWord()) {
    Serial.println("Wake word setup failed, stopping.");
    while (true) delay(1000);
  }
  connectWiFi();
  Serial.printf("Free heap: %u, largest block: %u\n", ESP.getFreeHeap(), ESP.getMaxAllocHeap());
  Serial.println("Calibrating noise floor, stay quiet for 2 seconds...");
  for (int i = 0; i < 200; i++) noiseFloor += (readChunk() - noiseFloor) * 0.05f;
  Serial.printf("Noise floor: %.0f. Say \"Hey Kate\".  (Serial: v0..v100 sets the volume)\n", noiseFloor);
  nextAnnounceTry = millis() + 2000;
}

void loop() {
  if (Serial.available() && Serial.read() == 'v') {
    volume = constrain(Serial.parseInt(), 0, 100);
    Serial.printf("volume = %d\n", volume);
  }

  float rms = readChunk();
  unsigned long now = millis();

  if (DEBUG_LEVELS) {
    static unsigned long lastPrint = 0;
    if (now - lastPrint > 500) {
      Serial.printf("rms=%.0f noise=%.0f wakeProb=%u/255 conn=%d\n", rms, noiseFloor, lastProb, (int)uploadReady);
      if (state == RECORDING) Serial.printf("   rec: env=%.0f endThr=%.0f speechLevel=%.0f heard=%d\n", envRms, curThr, speechLevel, (int)heardSpeech);
      lastPrint = now;
    }
  }

  if (state == LISTENING) {
    bool hit = feedWakeWord(pcm, CHUNK);          // always feed it, so old audio gets flushed out of the model
    if (now < cooldownUntil) return;              // ...but ignore detections during the cooldown
    if (rms < noiseFloor * 2) noiseFloor += (rms - noiseFloor) * 0.01f;

    // keep one upload connection open and ready, so "Hey Kate" can start streaming immediately
    if (!uploadReady) {
      if (now >= nextPrepare && WiFi.status() == WL_CONNECTED) {
        if (prepareUpload(300)) Serial.println(">> upload connection ready");
        else nextPrepare = millis() + RETRY_MS;   // server not reachable: try again in 30 s
      }
    } else if (!client.connected() || now - uploadOpenedAt > UPLOAD_REFRESH_MS) {
      refreshUpload();
    }

    if (hit) {
      Serial.println(">> Hey Kate detected, listening");
      if (!beginRecording(now)) {
        cooldownUntil = now + 3000;
        resetWakeWord();
      }
    } else if (WAIT_FOR_ANNOUNCEMENTS) {
      if (serviceAnnouncements(now)) afterPlayback(); // something was spoken
    }
  } else {  // RECORDING
    memcpy(sendBuf + sendFill, pcm, CHUNK * 2);
    sendFill += CHUNK;
    if (sendFill >= CHUNK * SEND_BATCH) {
      sendChunk((uint8_t*)sendBuf, sendFill * 2);
      sendFill = 0;
    }

    if (rms > recMaxRms) recMaxRms = rms;
    envRms += (rms - envRms) * 0.2f;                           // ~50 ms smoothing, so single clicks / Wi-Fi bursts don't count as speech
    float speechThr = max(SPEECH_MIN_RMS, noiseFloor * SPEECH_RATIO);
    if (envRms > speechLevel) speechLevel = envRms; else speechLevel *= 0.9995f;   // how loud you are, decays slowly
    // once you have spoken, "silence" must also be well below your own level (handles steady background noise)
    float endThr = heardSpeech ? max(speechThr, END_LEVEL_RATIO * speechLevel) : speechThr;
    curThr = endThr;
    if (envRms > endThr) { lastSpeech = now; heardSpeech = true; }
    else if (envRms < speechThr) noiseFloor += (envRms - noiseFloor) * 0.005f;   // learn the noise present while transmitting

    bool endBySilence = heardSpeech && (now - lastSpeech > SILENCE_MS);
    bool endNoSpeech  = !heardSpeech && (now - recStart > NO_SPEECH_MS);
    bool endTooLong   = (now - recStart > MAX_RECORD_MS);

    if (endBySilence || endNoSpeech || endTooLong) {
      if (sendFill > 0) sendChunk((uint8_t*)sendBuf, sendFill * 2);
      client.print("0\r\n\r\n");                       // end of upload
      cur = &client;
      Serial.printf(">> recorded %lu ms%s, loudest rms %.0f (speech threshold %.0f), waiting for reply\n",
                    now - recStart, heardSpeech ? "" : " (no speech heard)", recMaxRms, curThr);
      bool keepGoing = receiveAndPlayReply();
      state = LISTENING;
      afterPlayback();
      cooldownUntil = millis() + WAKE_COOLDOWN_MS;
      if (keepGoing) {                                // Kate asked something: no wake word needed
        delay(250);
        Serial.println(">> continuing the conversation, listening");
        if (beginRecording(millis())) cooldownUntil = 0;
      }
    }
  }
}
