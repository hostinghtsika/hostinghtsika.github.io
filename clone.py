import os
import re
import requests
import threading
import zipfile
import time
import logging
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser
import configparser
import telebot
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
import hashlib

# Konfigurasi logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_clone.log'),
        logging.StreamHandler()
    ]
)

# Konfigurasi
CONFIG_FILE = 'config.ini'
config = configparser.ConfigParser()

# Token dan Chat ID yang diberikan
BOT_TOKEN = "8038447034:AAHQIDsoq0f5Idn15ioXKLev0A24TQu1n5U"
ALLOWED_CHAT_ID = 7410636007

# Buat file config jika belum ada
if not os.path.exists(CONFIG_FILE):
    config['TELEGRAM'] = {
        'bot_token': BOT_TOKEN,
        'allowed_chat_id': str(ALLOWED_CHAT_ID)
    }
    config['SETTINGS'] = {
        'max_depth': '3',
        'max_workers': '8',
        'delay_between_requests': '0.5',
        'timeout': '45',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'chunk_size': '8192',
        'retry_attempts': '3'
    }
    with open(CONFIG_FILE, 'w') as f:
        config.write(f)
    print(f"File konfigurasi dibuat: {CONFIG_FILE}")
    print("Bot siap digunakan!")
else:
    config.read(CONFIG_FILE)
    BOT_TOKEN = config['TELEGRAM']['bot_token']
    ALLOWED_CHAT_ID = int(config['TELEGRAM']['allowed_chat_id'])

# Baca pengaturan tambahan
MAX_DEPTH = int(config['SETTINGS']['max_depth'])
MAX_WORKERS = int(config['SETTINGS']['max_workers'])
DELAY_BETWEEN_REQUESTS = float(config['SETTINGS']['delay_between_requests'])
TIMEOUT = int(config['SETTINGS']['timeout'])
USER_AGENT = config['SETTINGS']['user_agent']
CHUNK_SIZE = int(config['SETTINGS']['chunk_size'])
RETRY_ATTEMPTS = int(config['SETTINGS']['retry_attempts'])

bot = telebot.TeleBot(BOT_TOKEN)

# Session dengan custom headers
session = requests.Session()
session.headers.update({
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0'
})

# Fungsi untuk membersihkan nama file
def sanitize_filename(filename):
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip()

# Fungsi untuk memeriksa robots.txt
def can_fetch(url):
    try:
        parsed_url = urlparse(url)
        robots_url = urlunparse((parsed_url.scheme, parsed_url.netloc, '/robots.txt', '', '', ''))
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except Exception as e:
        logging.warning(f"Error checking robots.txt: {str(e)}")
        return True

# Fungsi untuk mengirim progress update
def send_progress(chat_id, message):
    try:
        bot.send_message(chat_id, message)
    except Exception as e:
        logging.error(f"Error sending progress: {str(e)}")

# Fungsi untuk mendownload dengan retry
def download_with_retry(url, stream=False, retries=RETRY_ATTEMPTS):
    for attempt in range(retries):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            if stream:
                response = session.get(url, stream=True, timeout=TIMEOUT)
            else:
                response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            return response
        except Exception as e:
            if attempt == retries - 1:
                raise
            logging.warning(f"Retry {attempt + 1} for {url}: {str(e)}")
            time.sleep(2 ** attempt)  # Exponential backoff

# Fungsi utama cloning website
def clone_website(url, chat_id):
    try:
        # Kirim pesan proses dimulai
        send_progress(chat_id, "🔄 Memulai proses cloning...\nMohon tunggu beberapa saat")

        # Periksa robots.txt
        if not can_fetch(url):
            send_progress(chat_id, "⚠️ Website ini melarang scraping di robots.txt")
            return

        # Buat folder khusus untuk chat ID
        base_dir = f"cloned_{chat_id}"
        os.makedirs(base_dir, exist_ok=True)
        
        # Parse domain
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        domain_folder = os.path.join(base_dir, sanitize_filename(domain))
        os.makedirs(domain_folder, exist_ok=True)
        
        # Set untuk melacak URL yang sudah diunduh
        visited_urls = set()
        resource_urls = set()
        url_queue = Queue()
        url_queue.put((url, 0))
        lock = threading.Lock()
        
        # Fungsi untuk mengunduh halaman
        def download_page_worker():
            while True:
                page_url, depth = url_queue.get()
                
                if depth > MAX_DEPTH:
                    url_queue.task_done()
                    continue
                
                with lock:
                    if page_url in visited_urls:
                        url_queue.task_done()
                        continue
                    visited_urls.add(page_url)
                
                try:
                    response = download_with_retry(page_url)
                    
                    # Tentukan path file
                    path = urlparse(page_url).path
                    if not path or path.endswith('/'):
                        path += 'index.html'
                    
                    local_path = os.path.join(domain_folder, path.lstrip('/'))
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    
                    # Simpan halaman
                    with open(local_path, 'w', encoding='utf-8') as f:
                        f.write(response.text)
                    
                    # Parse HTML
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Proses semua resource
                    for tag in soup.find_all(['img', 'script', 'link', 'source', 'iframe', 'video', 'audio', 'embed', 'object', 'track']):
                        attr = 'src' if tag.name in ['img', 'script', 'source', 'iframe', 'video', 'audio', 'embed', 'track'] else 'href'
                        if attr in tag.attrs:
                            resource_url = urljoin(page_url, tag[attr])
                            with lock:
                                if resource_url not in visited_urls:
                                    resource_urls.add(resource_url)
                    
                    # Proses semua link halaman
                    for link in soup.find_all('a', href=True):
                        next_url = urljoin(page_url, link['href'])
                        if urlparse(next_url).netloc == domain:
                            url_queue.put((next_url, depth + 1))
                            
                except Exception as e:
                    logging.error(f"Error downloading {page_url}: {str(e)}")
                
                url_queue.task_done()
        
        # Fungsi untuk mengunduh resource
        def download_resource_worker(resource_url):
            if resource_url in visited_urls:
                return
                
            with lock:
                visited_urls.add(resource_url)
            
            try:
                response = download_with_retry(resource_url, stream=True)
                
                # Tentukan path file
                path = urlparse(resource_url).path
                if not path:
                    return
                    
                local_path = os.path.join(domain_folder, path.lstrip('/'))
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                
                # Simpan resource
                with open(local_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        f.write(chunk)
                        
            except Exception as e:
                logging.error(f"Error downloading resource {resource_url}: {str(e)}")
        
        # Mulai proses
        send_progress(chat_id, f"📥 Mengunduh halaman utama: {url}")
        
        # Start worker threads untuk halaman
        page_workers = []
        for _ in range(MAX_WORKERS // 2):
            t = threading.Thread(target=download_page_worker)
            t.daemon = True
            t.start()
            page_workers.append(t)
        
        # Tunggu semua halaman selesai
        url_queue.join()
        
        send_progress(chat_id, f"📦 Ditemukan {len(resource_urls)} resource. Mulai mengunduh...")
        
        # Download resource secara paralel
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(download_resource_worker, url) for url in resource_urls]
            
            completed = 0
            for future in as_completed(futures):
                completed += 1
                if completed % 10 == 0 or completed == len(resource_urls):
                    send_progress(chat_id, f"⏳ Progress: {completed}/{len(resource_urls)} resource")
        
        # Buat file ZIP
        zip_filename = f"{domain_folder}.zip"
        send_progress(chat_id, "🗜️ Membuat file ZIP...")
        
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
            for root, _, files in os.walk(domain_folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, base_dir)
                    zipf.write(file_path, arcname)
        
        # Kirim file ZIP
        send_progress(chat_id, "✅ Cloning selesai! Mengirim file...")
        with open(zip_filename, 'rb') as f:
            bot.send_document(chat_id, f, caption=f"✅ Cloning selesai!\nWebsite: {domain}\nTotal file: {len(visited_urls)}\nUkuran ZIP: {os.path.getsize(zip_filename)/1024/1024:.2f} MB")
        
        # Hapus file sementara
        os.remove(zip_filename)
        for root, dirs, files in os.walk(base_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(base_dir)
        
    except Exception as e:
        logging.error(f"Error in clone_website: {str(e)}")
        send_progress(chat_id, f"❌ Terjadi kesalahan: {str(e)}")

# Handler pesan
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.chat.id != ALLOWED_CHAT_ID:
        bot.reply_to(message, "⚠️ Anda tidak diizinkan menggunakan bot ini")
        return
        
    if message.text.startswith('http'):
        # Jalankan di thread terpisah agar tidak blocking
        threading.Thread(target=clone_website, args=(message.text, message.chat.id)).start()
    else:
        bot.reply_to(message, "🌐 Silakan kirim link website yang ingin di-clone\nContoh: https://example.com")

if __name__ == '__main__':
    print("Bot berjalan...")
    print(f"Chat ID yang diizinkan: {ALLOWED_CHAT_ID}")
    print("Kirim link website ke bot untuk mulai cloning")
    bot.polling()