EXAM VIEWER — macOS
===================

Mac-də yığmaq:
  1. python.org-dan Python 3.12 və ya 3.13 qur.
  2. Terminal-da bu qovluğa gir və işlət:   bash build_mac.sh
  Hazır olur:  dist/ExamViewer.app
               dist/ExamViewer-mac-arm64.dmg   (Intel Mac-də: -x86_64)

Mac yoxdursa (GitHub ilə):
  Bu qovluğu GitHub repo-suna yüklə, build-mac.yml faylını .github/workflows/ qovluğuna köçür,
  Actions → "macOS build" → Run workflow. .dmg faylları Artifacts bölməsində olur.

İlk açılışda Mac proqramı bloklayırsa:
  System Settings → Privacy & Security → "Open Anyway".

Tarif / qiymət / kart / e-poçt dəyişmək:
  license_guard.py → SALES bölməsi. Dəyişəndən sonra yenidən yığ.

DİQQƏT: launcher.py, license_guard.py, parse_api.py, parse_exam_pdf.py, viewer.html
windows qovluğunda da var. Birində dəyişiklik edəndə digərində də et.

TƏHLÜKƏSİZLİK
=============
Qoyulmuş qorumalar:
 - Lisenziya yoxlaması Python tərəfdədir (viewer.html yox). HTML-i dəyişmək və ya
   DevTools açmaq lisenziyadan yan keçməyə imkan vermir — API açarsız cavab vermir.
 - 1 açar = 1 kompüter (sabit hardware ID). Açarlı faylı başqa Mac-ə köçürmək işləmir.
 - Hər açılışda təsadüfi sessiya kukisi: yalnız proqramın öz pəncərəsi lisenziya/admin
   sorğusu göndərə bilir, kənar skript yox.
 - API strukturu (/docs, /openapi.json) bağlıdır.
 - Admin sessiyası Firebase ID token ilə serverdə yoxlanılır.

Sən etməlisən:
 - Admin parolu güclü olsun (admin bütün açarları yarada bilir — parol sızsa, pulsuz açar
   yaradıla bilər). Qoyduğun "Qlad!ator2008" olar, amma heç kimə demə.
 - Google Cloud Console → APIs & Services → Credentials → Web API key-i yalnız
   "Identity Toolkit API" və "Cloud Firestore API" ilə məhdudlaşdır.

Dürüst xəbərdarlıq:
 Kompüterdə işləyən HEÇ bir proqram 100% crack-dən qorunmur — exe-ni açıb Python kodu
 çıxarmaq mümkündür. Yuxarıdakılar adi tələbənin qıra bilməməsi üçün kifayətdir.
 Daha güclü qorumaya ehtiyac olsa: kodu PyArmor ilə gizlətmək olar (istəyə bağlı):
     pip install pyarmor
     pyarmor gen -O obf launcher.py license_guard.py parse_api.py parse_exam_pdf.py
 sonra obf/ qovluğundakı fayllarla yığmaq. (Default build-a lazım deyil.)
