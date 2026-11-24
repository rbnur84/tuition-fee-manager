TUITION FEE MANAGER — MOBILE WEB APP / PWA
=========================================

এটি ফোনে ব্যবহারের জন্য তৈরি responsive web app। HTTPS-এ হোস্ট করার পর Android Chrome-এ খুলে “Add to Home Screen” করলে অ্যাপের মতো আইকন থেকে খোলা যাবে। এটি APK নয়।

যা করতে পারবে
--------------
1) Student ID ও অভিভাবকের মোবাইল নম্বর সংরক্ষণ।
2) ফোনের পেছনের ক্যামেরা দিয়ে QR scan করে শিক্ষার্থী খোঁজা।
3) মাস নির্বাচন করে বেতন Paid করা।
4) Payment record সংরক্ষণ এবং gateway configure থাকলে সঙ্গে সঙ্গে confirmation SMS পাঠানোর চেষ্টা।
5) প্রতি মাসের ১৫ তারিখ থেকে বকেয়া শিক্ষার্থীদের একবার করে reminder SMS পাঠানো।
6) Paid/Unpaid report, CSV import/export এবং database backup।

গুরুত্বপূর্ণ বাস্তব শর্ত
----------------------
- ফোনে camera QR scan চালাতে app-টি HTTPS ওয়েব ঠিকানায় host করতে হবে; সাধারণ HTTP IP address-এ browser camera বন্ধ রাখতে পারে।
- ১৫ তারিখের স্বয়ংক্রিয় SMS পাঠাতে server অনলাইনে ২৪/৭ চালু থাকতে হবে। শুধু ফোনের Home Screen-এ app install করলেই background SMS চলবে না।
- Cloud hosting-এ persistent disk/database সেট করতে হবে, যাতে restart হলেও student/payment data না হারায়। DB_PATH সেট করে database path নির্ধারণ করো।
- একটিমাত্র web worker চালাও; scheduler thread একাধিক worker-এ duplicate হবে। Procfile-এ workers=1 দেওয়া আছে।
- Public hosting-এর আগে ADMIN_PASSWORD, FLASK_SECRET অবশ্যই শক্তিশালী মানে সেট করো। REQUIRE_LOGIN=1 রাখো।
- SMS provider-এর API format আলাদা হতে পারে। Provider documentation অনুযায়ী send_sms() payload mapping যাচাই না করা পর্যন্ত বাস্তব SMS পাঠানো হয়েছে ধরে নিও না।

Cloud deployment (সাধারণ নির্দেশনা)
----------------------------------
1) ZIP Extract করে project folder Git repository-তে upload করো অথবা hosting provider-এ deploy করো।
2) Python 3.10+ runtime, build command: pip install -r requirements.txt
3) Start command: gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app
4) Persistent storage mount করে DB_PATH-এ তার path দাও।
5) Environment Variables-এ FLASK_SECRET, ADMIN_PASSWORD, REQUIRE_LOGIN=1, DB_PATH সেট করো।
6) HTTPS domain চালু হলে ফোনের Chrome-এ খুলে Login করো, তারপর browser menu > Add to Home screen নির্বাচন করো।
7) Settings-এ provider অনুযায়ী SMS API সেট করো। আগে নিজের নম্বরে test SMS করো।

QR scan
-------
- Student list-এ প্রত্যেক শিক্ষার্থীর QR image পাওয়া যায়; সেটি ID card-এ print করো।
- Scan পেজে “ফোনের ক্যামেরা দিয়ে QR Scan” চাপো এবং camera permission Allow করো।
- QR-এর ভেতরে Student ID থাকতে হবে, যেমন ST-001।
- BarcodeDetector সমর্থিত browser (সাম্প্রতিক Android Chrome) ব্যবহার করো। না হলে Student ID হাতে লিখতে পারবে।

স্থানীয় পরীক্ষা
--------------
Windows-এ Python 3.10+ ইনস্টল করে start.bat চালাও। Local browser-এ http://127.0.0.1:5000 খুলবে। ফোন থেকে একই Wi-Fi-তে local IP দিয়ে dashboard দেখা যেতে পারে, তবে camera scan-এর জন্য HTTPS দরকার।

SMS এবং reminder নিয়ম
---------------------
- Payment record আগে database-এ লেখা হয়; তারপর confirmation SMS পাঠানোর চেষ্টা হয়।
- Reminder প্রতি মাসের ১৫ তারিখ থেকে server চলাকালীন unpaid list check করে।
- একই শিক্ষার্থী, একই মাস ও একই reminder type-এর জন্য duplicate attempt block করা আছে। failed SMS-ও retry না করলে দ্বিতীয় automatic attempt হবে না; Reports & SMS থেকে manual retry করা যায়।
- Provider-specific SMS integration configure ও test না করা পর্যন্ত SMS feature প্রস্তুত বলা যাবে না।

ডাটা নিরাপত্তা
-------------
Student phone number ও payment history ব্যক্তিগত তথ্য। শক্তিশালী admin password, HTTPS, private repository এবং নিয়মিত database backup ব্যবহার করো।
