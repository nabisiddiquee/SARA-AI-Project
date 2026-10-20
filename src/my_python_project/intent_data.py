"""Built-in training examples for Sara's intent model.

Each intent maps to example commands in English, Hinglish, and Hindi. The
``chat`` intent is just as important as the action intents: it teaches the
model that questions which merely *mention* an action word ("exit code kya
hai", "how do I quit smoking", "shutdown kaise karte hain") are conversation
for the AI brain, not commands to run.

Add your own phrases to ``my_training_data.json`` in the project folder (or
use ``python -m my_python_project teach "<text>" <intent>``) and retrain with
``python -m my_python_project train``.
"""

from __future__ import annotations

import random

INTENT_EXAMPLES: dict[str, list[str]] = {
    "greeting": [
        "hello", "hi", "hey", "hey sara", "hi sara", "hello sara", "namaste",
        "namaste sara", "namaskar", "salam", "good morning", "good evening",
        "good afternoon", "hey there", "hello ji", "hi there", "yo sara",
        "नमस्ते", "हेलो", "हाय सारा", "welcome", "swagat hai", "kaise ho sara",
        "kaise ho", "how are you", "how are you sara", "sara kaisi ho",
    ],
    "identity": [
        "who are you", "what is your name", "your name", "tell me about you",
        "tum kaun ho", "aap kaun ho", "tumhara naam kya hai", "aapka naam kya hai",
        "apna naam batao", "tum kon ho", "introduce yourself", "about you",
        "तुम कौन हो", "आप कौन हैं", "what can you do", "tum kya kar sakti ho",
        "my name is nabi", "mera naam nabi hai", "मेरा नाम नबी है",
    ],
    "time": [
        "what time is it", "time kya hai", "abhi kya time hai", "kitne baje hain",
        "abhi kitne baje hai", "current time", "tell me the time", "time batao",
        "samay kya hai", "aaj ki date kya hai", "what is today's date", "today date",
        "aaj kaun sa din hai", "what day is it today", "time in new york",
        "london mein kya time hai", "time in dubai", "tareekh batao",
        "समय क्या है", "अभी कितने बजे हैं", "आज की तारीख", "clock dikhao",
        "waqt kya hua hai", "date and time",
    ],
    "weather": [
        "weather", "what's the weather", "weather today", "weather in london",
        "darbhanga ka mausam", "aaj ka mausam kaisa hai", "mausam batao",
        "delhi ka mausam", "will it rain today", "kya aaj baarish hogi",
        "temperature kitna hai", "how hot is it", "weather in patna",
        "mumbai weather", "मौसम कैसा है", "आज का मौसम", "तापमान कितना है",
        "is it cold outside", "bahar mausam kaisa hai", "forecast for tomorrow",
    ],
    "news": [
        "news", "latest news", "aaj ki taza khabar", "khabar sunao",
        "today's headlines", "cricket news", "cricket ki news", "sports news",
        "tech news", "news about india", "samachar sunao", "breaking news",
        "business news batao", "खबर सुनाओ", "आज का समाचार", "ताज़ा खबरें",
        "what's happening in the world", "headlines dikhao", "bihar ki khabar",
    ],
    "open_app": [
        "open chrome", "chrome kholo", "open postman", "postman open karo",
        "open vs code", "vs code kholo", "open vlc", "vlc chalu karo",
        "launch spotify", "open calculator", "calculator kholo", "open notepad",
        "open notepad++", "open intellij", "open openvpn", "open file explorer",
        "open settings", "settings kholo", "start edge", "open microsoft edge",
        "open youtube", "youtube kholo", "youtube chalao", "play youtube",
        "open command prompt", "cmd kholo", "launch mongodb compass",
        "requestly open karo", "open word", "excel kholo", "open whatsapp app",
        "chrome खोलो", "यूट्यूब खोलो", "कैलकुलेटर चालू करो", "run paint",
        "start telegram", "open the browser", "browser kholo",
    ],
    "close_app": [
        "close chrome", "chrome band karo", "close postman", "postman band karo",
        "close vs code", "quit spotify", "exit vlc", "kill notepad",
        "close calculator", "calculator band kardo", "close the browser",
        "browser band karo", "close edge", "band karo chrome", "exit postman",
        "close teams", "teams band karo", "close word", "excel band karo",
        "chrome बंद करो", "नोटपैड बंद करो", "quit telegram", "close file explorer",
        "kill the chrome process", "stop spotify", "vlc band kar do",
    ],
    "power": [
        "shutdown", "shut down the computer", "shutdown the system",
        "system band karo", "computer band karo", "laptop band kar do",
        "pc shutdown karo", "restart", "restart the computer", "system restart karo",
        "reboot the pc", "laptop restart karo", "sleep", "put the computer to sleep",
        "system ko sleep karo", "laptop sula do", "lock", "lock the computer",
        "lock my pc", "system lock karo", "screen lock karo",
        "shutdown after 2 hours", "1 ghante baad shutdown karo",
        "restart in 10 minutes", "cancel shutdown", "shutdown roko",
        "restart cancel karo", "सिस्टम बंद करो", "कंप्यूटर रीस्टार्ट करो",
        "लैपटॉप लॉक करो",
    ],
    "create_fs": [
        "create folder projects", "make a folder called notes", "new folder test",
        "folder banao", "ek folder banao", "desktop par folder banao photos",
        "create folder in d drive named backup", "create file notes.txt",
        "make a file report.txt", "file banao", "notes.txt file bana do",
        "naya folder banao", "downloads mein folder banao music",
        "create a new file todo.txt on desktop", "फोल्डर बनाओ", "फाइल बनाओ",
        "d drive me college naam ka folder banao", "make directory data",
    ],
    "list_fs": [
        "list desktop", "desktop dikhao", "d drive ke folders dikhao",
        "show files in downloads", "downloads mein kya hai", "list c drive",
        "what is in my documents", "documents ke files dikhao",
        "show me the desktop files", "list folders in d drive",
        "e drive dikhao", "folder ke contents dikhao", "list files",
        "डेस्कटॉप दिखाओ", "show contents of pictures",
    ],
    "whatsapp": [
        "open whatsapp", "whatsapp kholo", "whatsapp chalu karo",
        "send whatsapp to 9876543210 hello", "9876543210 ko message karo hello",
        "mummy ko message karo aa raha hu", "papa ko whatsapp karo",
        "message bhejo rahul ko", "send a message to 9123456789 meeting at 5",
        "whatsapp message bhejo", "bhai ko msg karo khana kha liya",
        "व्हाट्सएप खोलो", "whatsapp pe message bhejo", "text 9988776655 call me",
        "didi ko msg bhejo ghar aa gaya", "bhai ko message bhej do late ho jaunga",
        "papa ko msg karo khana ready hai", "send a message to mom i am coming",
        "rahul ko bolo meeting cancel hai", "dost ko whatsapp pe hello bhejo",
        "mummy ko msg bhejo", "wife ko message karo dudh le aaunga",
        "9876543210 pe msg bhejo", "message karo ammi ko",
    ],
    "email": [
        "email bhejo", "send an email", "abc@gmail.com ko email bhejo meeting kal hai",
        "email rahul@gmail.com that the report is ready", "boss ko email karo chhutti chahiye",
        "mail bhejo teacher ko assignment submit kar diya", "send a mail to hr about leave",
        "ek email likho boss ko", "write an email to my manager i will be late",
        "didi ko mail kar do birthday ki badhai", "email karo sir ko kal nahi aaunga",
        "send email to test@example.com hello", "ईमेल भेजो", "मेल भेजो बॉस को",
        "gmail se mail bhejo", "draft an email to the team about the meeting",
        "email kar do papa ko", "mail send karo client ko invoice bhej diya",
    ],
    "stock": [
        "nifty kitna hai", "sensex kya hai aaj", "share market kaisa hai",
        "stock market update", "market kaisa chal raha hai", "tata motors share price",
        "reliance ka share price", "tcs stock price", "infosys share kitne ka hai",
        "apple stock price", "bank nifty batao", "aaj market up hai ya down",
        "sensex aur nifty dikhao", "hdfc bank ka share", "gold rate kya hai",
        "tesla stock", "adani ke share ka bhav", "nasdaq kaisa hai", "dow jones today",
        "शेयर बाजार कैसा है", "निफ्टी कितना है", "सेंसेक्स बताओ", "sbi share price",
        "zomato stock", "bitcoin price", "dollar ka rate kya hai",
    ],
    "camera": [
        "open camera", "camera kholo", "start camera", "camera chalu karo",
        "close camera", "camera band karo", "stop camera", "turn on the camera",
        "turn off camera", "webcam kholo", "कैमरा खोलो", "कैमरा बंद करो",
        "open cam", "cam on karo",
    ],
    "game": [
        "play a game", "game khelo", "let's play a game", "number guessing game",
        "guess the number", "roll a dice", "roll the dice", "dice roll karo",
        "flip a coin", "toss a coin", "coin toss karo", "rock paper scissors",
        "rock", "paper", "scissors", "quiz", "start a quiz", "quiz khelo",
        "trivia", "tic tac toe", "tic tac toe khelo", "khel khelte hai",
        "गेम खेलो", "पासा फेंको", "सिक्का उछालो",
    ],
    "animation": [
        "animation", "show animation", "matrix", "matrix rain", "binary rain",
        "binary animation", "animation chalao", "matrix dikhao",
        "एनिमेशन दिखाओ", "start the matrix animation",
    ],
    "chat": [
        # Questions that only *mention* action words -> must go to the AI.
        "exit code 1 kya hai", "what does exit code 1 mean",
        "how do i quit smoking", "how to quit a job politely",
        "dukaan band hai kya", "kya aaj bank band hai", "school kab band hoga",
        "kill python process kaise karte hain", "how to kill a process in linux",
        "shutdown kaise karte hain", "what is the shutdown command in linux",
        "restart karne se kya hota hai", "why does my laptop restart by itself",
        "how to create a folder in python", "python me file kaise banate hain",
        "what is a file system", "folder aur file me kya fark hai",
        "how to open a file in python", "open source kya hai",
        "what is open source software", "startup ideas batao",
        "how to start a business", "chrome fast kaise kare",
        "which is better chrome or edge", "whatsapp ka malik kaun hai",
        "who invented the camera", "camera kaise kaam karta hai",
        "what is the time complexity of binary search",
        "time management tips", "time kaise manage kare",
        "what is climate change", "news channel kaise shuru kare",
        "what is a game engine", "game development kaise sikhe",
        "what is a matrix in maths", "list comprehension in python",
        "what is a linked list", "close friend ko kya gift de",
        "how do i close a bank account", "exit poll kya hota hai",
        "band baaja baraat movie kaisi hai", "rock music history",
        # General knowledge / conversation.
        "what is the capital of france", "capital of india kya hai",
        "tell me a joke", "ek joke sunao", "write a poem", "ek kavita sunao",
        "what is python", "python kya hai", "explain machine learning",
        "machine learning kya hai", "how does the internet work",
        "who is the prime minister of india", "bharat ke pradhan mantri kaun hai",
        "what is 25 times 4", "25 guna 4 kitna hota hai", "translate hello to hindi",
        "motivate me", "mujhe motivate karo", "i am feeling sad", "main udaas hu",
        "give me a recipe for tea", "chai kaise banate hain", "what is love",
        "tell me a fun fact", "koi interesting baat batao", "how far is the moon",
        "chand kitni door hai", "who won the world cup in 2011",
        "explain quantum computing simply", "what should i learn next",
        "suggest a good book", "koi achhi movie batao", "how to lose weight",
        "vitamin d ki kami kaise door kare", "write an email to my boss",
        "what is ai", "ai kya hai",
        "email kya hota hai", "how to write a formal email", "email id kaise banaye",
        "gmail ka password bhool gaya", "what is spam mail", "email aur sms me fark",
        "stock market kaise kaam karta hai", "share market kaise sikhe",
        "what is a stock", "should i invest in mutual funds", "sip kya hota hai",
        "stock market me paisa kaise lagaye", "what is nifty", "sensex kya hota hai",
        "intraday trading kya hai", "is investing in shares safe", "can you help me with my homework",
        "meaning of life", "thank you", "shukriya", "dhanyavaad", "ok", "okay",
        "accha", "thik hai", "you are smart", "tum bahut achhi ho",
        "क्या हाल है दुनिया का", "भारत की राजधानी क्या है", "एक चुटकुला सुनाओ",
    ],
}


# --- Generated examples ------------------------------------------------------
# Pairs of commands and questions built from the same words, so the model learns
# that a question word ("kaise", "kya", "how", "why") turns an action into chat.

_APPS = (
    "chrome", "edge", "firefox", "vlc", "spotify", "postman", "vs code", "notepad",
    "calculator", "teams", "zoom", "telegram", "word", "excel", "powerpoint",
    "paint", "file explorer", "settings", "intellij", "discord", "outlook",
    "mongodb compass", "docker", "cursor", "antigravity", "brave", "obs",
)
_OPEN = ("open {a}", "{a} kholo", "{a} khol do", "launch {a}", "start {a}",
         "{a} open karo", "{a} chalu karo", "{a} chalao", "run {a}", "please open {a}",
         "{a} खोलो")
_CLOSE = ("close {a}", "{a} band karo", "{a} band kar do", "{a} band kr",
          "quit {a}", "exit {a}", "kill {a}", "stop {a}", "{a} close karo",
          "please close {a}", "{a} बंद करो", "band karo {a}")
_APP_QUESTIONS = (
    "{a} kya hai", "what is {a}", "how to use {a}", "{a} kaise use kare",
    "how do i close {a} on mac", "{a} band kaise karte hain", "why is {a} so slow",
    "{a} hang kyu hota hai", "how to exit {a} fullscreen", "is {a} free",
    "{a} ke features batao", "who made {a}", "{a} update kaise kare",
    "how to kill {a} when it freezes", "{a} open kyu nahi ho raha",
    "{a} vs firefox which is better", "{a} kitni ram leta hai",
)
_POWER_CMDS = (
    "shutdown karo", "shut down", "system shutdown", "pc band karo", "laptop band kr do",
    "computer shutdown kar do", "restart karo", "system reboot", "pc restart kr do",
    "laptop ko sleep mode me dalo", "sleep mode on karo", "lock screen", "pc lock kr do",
    "shutdown in 30 minutes", "2 ghante baad system band karo",
    "restart after 5 minutes", "shutdown cancel karo", "cancel the restart",
)
_POWER_QUESTIONS = (
    "shutdown aur restart me kya fark hai", "why is my pc shutting down randomly",
    "how to schedule shutdown in windows", "sleep mode kya hota hai",
    "how much sleep do i need", "neend kyu nahi aati", "lock screen wallpaper kaise badle",
    "restart karne se ram clear hoti hai kya", "what is hibernate vs sleep",
    "laptop garam kyu hota hai", "computer slow kyu hai", "how to lock a folder",
)
_FS_QUESTIONS = (
    "how to make a folder private", "file ko zip kaise kare", "what is a pdf file",
    "folder ka size kaise dekhe", "how to list files in linux", "ls command kya karta hai",
    "list vs tuple in python", "create react app kya hai", "how to create a website",
    "new year kab hai", "make me laugh", "make a plan for my study",
)
_LIVE_EXTRA = {
    "time": ("abhi time kya hua hai", "kitna baj gaya", "time bata do", "what's the time now",
             "tokyo mein time kya hai", "aaj kya tareekh hai", "which day is today",
             "aaj konsa din hai", "समय बताओ"),
    "weather": ("mausam kaisa hai", "aaj garmi kitni hai", "baarish hogi kya aaj",
                "weather kaisa hai delhi me", "patna mausam", "how is the weather",
                "temperature batao", "is it going to rain", "aaj thand hai kya"),
    "news": ("news sunao", "aaj ki news", "latest khabar", "top headlines", "world news",
             "india news today", "politics news", "news batao"),
    "identity": ("tumhe kisne banaya", "who made you", "what are you", "are you a robot",
                 "tum kya ho", "aap kya kar sakti ho", "your introduction"),
    "greeting": ("hello ji", "hii", "helo", "hey sara kaise ho", "good night",
                 "namaste ji", "salaam", "hi there sara"),
}


def _generated() -> dict[str, list[str]]:
    rng = random.Random(42)
    out: dict[str, list[str]] = {
        "open_app": [], "close_app": [], "chat": [], "power": list(_POWER_CMDS),
    }
    for a in _APPS:
        out["open_app"] += [t.format(a=a) for t in rng.sample(_OPEN, 3)]
        out["close_app"] += [t.format(a=a) for t in rng.sample(_CLOSE, 4)]
        out["chat"] += [t.format(a=a) for t in rng.sample(_APP_QUESTIONS, 3)]
    out["chat"] += list(_POWER_QUESTIONS) + list(_FS_QUESTIONS)
    for intent, texts in _LIVE_EXTRA.items():
        out[intent] = list(texts)
    return out


for _intent, _texts in _generated().items():
    INTENT_EXAMPLES.setdefault(_intent, []).extend(_texts)
