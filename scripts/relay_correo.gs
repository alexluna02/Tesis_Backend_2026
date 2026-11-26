// AgroScan · relay de correo (Google Apps Script)
//
// Hugging Face Spaces bloquea el correo saliente por SMTP, así que el backend envía los correos
// (p. ej. el código de recuperación de contraseña) por HTTPS a este script, que los manda desde la
// cuenta de Gmail que lo publica. Límite de Gmail: unos 100 correos al día.
//
// Cómo publicarlo:
//  1. Entra a https://script.google.com con la cuenta de Gmail que enviará los correos →
//     «Nuevo proyecto». Borra lo que haya y pega este archivo.
//  2. Reemplaza SECRETO por una clave larga (la misma irá en MAIL_RELAY_SECRET del Space).
//  3. «Implementar» → «Nueva implementación» → tipo «Aplicación web»:
//       Ejecutar como: Yo · Quién tiene acceso: Cualquier usuario → «Implementar».
//     Google pedirá autorizar el envío de correos: «Revisar permisos» → elige la cuenta →
//     «Configuración avanzada» → «Ir a … (no seguro)» → «Permitir» (es tu propio script).
//  4. Copia la URL de la aplicación web (termina en /exec).
//  5. En Hugging Face: Space → Settings → «Variables and secrets» → «New secret»:
//       MAIL_RELAY_URL    = la URL del paso 4
//       MAIL_RELAY_SECRET = la clave del paso 2
//     El Space se reinicia solo. En /health debe aparecer "email_transport": "relay".
//
// Si cambias el código del script, publica una versión nueva en la misma implementación
// («Gestionar implementaciones» → editar → «Nueva versión») para conservar la URL.

const SECRETO = 'PEGA_AQUI_LA_CLAVE_DE_MAIL_RELAY_SECRET'

function doPost(e) {
  try {
    const datos = JSON.parse(e.postData.contents)
    if (!SECRETO || SECRETO.startsWith('PEGA_AQUI') || datos.secret !== SECRETO) {
      return ContentService.createTextOutput('error: secreto incorrecto')
    }
    if (!datos.to || !datos.subject || !datos.html) {
      return ContentService.createTextOutput('error: faltan datos')
    }
    MailApp.sendEmail({ to: datos.to, subject: datos.subject, htmlBody: datos.html, name: 'AgroScan' })
    return ContentService.createTextOutput('ok')
  } catch (err) {
    return ContentService.createTextOutput('error: ' + err)
  }
}
