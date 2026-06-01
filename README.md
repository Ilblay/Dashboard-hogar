# Dashboard Hogar - Deploy en GitHub Actions + GitHub Pages

Sistema automatizado de monitoreo de consumo electrico que corre en los
servidores de GitHub (24/7, sin necesidad de PC encendido) y publica el
dashboard en una URL accesible desde cualquier dispositivo.

## Pasos para subirlo (una sola vez)

### 1. Crea cuenta de GitHub (si no tienes)

Ve a https://github.com/signup → registrate con tu email. Es gratis.

### 2. Crea un repositorio nuevo

1. En GitHub haz clic en el **"+"** arriba a la derecha → **"New repository"**
2. Nombre: `dashboard-hogar`
3. Descripcion: opcional
4. **Tipo: Publico** (necesario para que GitHub Pages gratis funcione)
5. NO marcar "Add a README", "Add .gitignore" ni "Add license" (los archivos vienen con el repo local)
6. Clic en **"Create repository"**

### 3. Sube los archivos al repo

GitHub te va a mostrar instrucciones. Tienes 2 opciones:

**Opcion A - Mediante la web (mas facil):**

1. En la pagina del repo recien creado, haz clic en **"uploading an existing file"**
2. Arrastra TODOS los archivos de esta carpeta (`github_deploy/`) hacia la pagina
   - IMPORTANTE: incluye la carpeta `.github/` aunque parezca oculta
3. Abajo en "Commit message": `Setup inicial`
4. Clic en **"Commit changes"**

**Opcion B - Usando Git desde la consola** (requiere instalar Git):

```bash
cd ruta/a/github_deploy
git init
git add .
git commit -m "Setup inicial"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/dashboard-hogar.git
git push -u origin main
```

### 4. Configura las credenciales como Secrets

**MUY IMPORTANTE:** las credenciales de Tuya NO se suben al repo. Se guardan
encriptadas en GitHub.

1. En el repo, ve a **Settings** (engranaje arriba)
2. En el menu izquierdo: **Secrets and variables → Actions**
3. Clic en **"New repository secret"**
4. Agrega el primer secret:
   - Name: `TUYA_ACCESS_ID`
   - Value: tu Access ID de Tuya (lo tienes en `config.json` local, NO el placeholder)
   - Clic en **"Add secret"**
5. Agrega el segundo secret:
   - Name: `TUYA_ACCESS_SECRET`
   - Value: tu Access Secret de Tuya
   - Clic en **"Add secret"**

### 5. Activa GitHub Pages

1. Settings → en el menu izquierdo: **Pages**
2. Source: **Deploy from a branch**
3. Branch: **main** / Folder: **/docs**
4. Clic en **Save**
5. Espera ~1 minuto. Arriba aparecera la URL del dashboard:
   `https://TU_USUARIO.github.io/dashboard-hogar/`

### 6. Ejecuta el workflow por primera vez

1. Ve a la pestaña **"Actions"** del repo
2. Click en **"Update Dashboard Hogar"** (en la barra izquierda)
3. Click en **"Run workflow"** → **"Run workflow"** (boton verde)
4. Espera ~1 minuto. El workflow corre, llama a Tuya, genera el dashboard y lo publica
5. Recarga la URL de GitHub Pages → veras el dashboard actualizado

A partir de aqui, **cada 15 minutos** el dashboard se actualiza solo en
los servidores de GitHub, sin que tu PC ni tu celular necesiten estar
encendidos.

## Como acceder al dashboard desde el celular

1. Abre Chrome (o Safari) en tu celular
2. Visita: `https://TU_USUARIO.github.io/dashboard-hogar/`
3. Para que quede como "app":
   - En Chrome (Android): menu (3 puntos) → **"Agregar a pantalla principal"**
   - En Safari (iPhone): boton compartir → **"Agregar a inicio"**
4. Ahora tienes un icono en tu celular que abre el dashboard como si fuera una app

## Como cambiar la frecuencia de actualizacion

Edita `.github/workflows/update.yml`, busca esta linea:

```yaml
- cron: '*/15 * * * *'   # cada 15 min
```

Cambia el numero. Por ejemplo:
- `*/5 * * * *` = cada 5 min (mas frecuente, mas API calls)
- `*/30 * * * *` = cada 30 min
- `0 * * * *` = cada hora exacta

GitHub permite hasta 2000 minutos/mes de Actions gratis para repos privados,
y **ilimitado** para repos publicos. Cada ejecucion son ~30 segundos.

## Como actualizar tus datos manuales (consumo de la app Smart Life)

Si quieres recalibrar con datos exactos:

1. En el repo, edita el archivo `config.json` directamente desde la web
2. Modifica la seccion `consumo_diario_manual.datos`
3. Commit & push (un boton en la web)
4. El proximo ejecucion del workflow usara los nuevos valores

## Estructura del proyecto

| Archivo | Para que sirve |
|---|---|
| `.github/workflows/update.yml` | Workflow que corre el script cada 15 min |
| `fetch_data.py` | Script Python que consulta Tuya y genera el dashboard |
| `dashboard_template.html` | Template HTML del dashboard |
| `config.json` | Configuracion (dispositivos, tarifa, presupuesto, datos manuales) |
| `requirements.txt` | Dependencia: `requests` |
| `data/historico.json` | Datos historicos de muestras (lo actualiza el workflow) |
| `docs/index.html` | Dashboard publicado (lo genera el workflow) |

## Privacidad

- El repo es **publico** (necesario para GH Pages gratis), pero las credenciales
  de Tuya son **secrets** y NO se ven publicamente.
- Lo que se ve publicamente: tus datos de consumo electrico en kWh. No hay
  informacion personal identificable (ni tu nombre, direccion, ni nada).
- Si te preocupa que se vean los kWh: paga $4/mes por GitHub Pro y puedes hacer
  el repo privado manteniendo GH Pages activo.

## Troubleshooting

**El workflow falla con "401 Unauthorized":**
- Revisa que los Secrets TUYA_ACCESS_ID y TUYA_ACCESS_SECRET esten bien
- Si regeneraste las credenciales en Tuya, actualizalas

**El dashboard no se actualiza:**
- Ve a Actions → revisa si la ultima ejecucion falla
- Si dice "skipping" o "no changes": Tuya no entrego datos nuevos en este ciclo

**Quiero deshabilitar temporalmente el workflow:**
- Actions → Update Dashboard Hogar → menu (...) → Disable workflow

## Mantenimiento

- Una vez configurado, esto se ejecuta solo para siempre
- Los datos del periodo viejo se mantienen en `historico.json`
- Cuando llega el nuevo periodo (24 de cada mes), el dashboard automaticamente
  cambia el rango mostrado
- Si quieres actualizar la tarifa con la siguiente boleta, edita `config.json`
  en la web

---

**Nota:** Las credenciales de Tuya en el `config.json` de este repo son placeholders
(`PUT_IT_AS_GITHUB_SECRET`). Las reales viven SOLO como GitHub Secrets cifrados.
El script lee las env vars `TUYA_ACCESS_ID` y `TUYA_ACCESS_SECRET` cuando estan disponibles.
