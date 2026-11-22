# AGENT.md — MusicOrder

## Control de versiones y sincronización automática con GitHub

### Repositorio

- Proyecto: MusicOrder
- Directorio: /mnt/R1/HDD500GB/MusicOrder
- Rama principal: main
- Remoto: origin
- Repositorio GitHub: git@github.com:juanmanuelubilla/MusicOrder.git
- Autenticación: SSH.

### REGLA OBLIGATORIA

Después de completar cada tarea solicitada por el usuario, debés sincronizar automáticamente los cambios con GitHub.

El usuario no necesita solicitar explícitamente un commit o un push.

### Procedimiento obligatorio

1. Ejecutar `git status` para identificar los cambios.
2. Revisar `git diff` para comprobar las modificaciones.
3. Verificar que no existan credenciales, contraseñas, tokens, archivos `.env` ni información sensible.
4. Incorporar únicamente los archivos correspondientes a la tarea realizada.
5. Crear un commit con un mensaje descriptivo.
6. Ejecutar `git push origin main`.
7. Verificar que el repositorio local esté sincronizado con GitHub.

### Convención de commits

Utilizar Conventional Commits:

- feat: nuevas funcionalidades.
- fix: correcciones.
- refactor: reorganización del código.
- perf: mejoras de rendimiento.
- test: pruebas.
- docs: documentación.
- chore: mantenimiento.

### Seguridad

- Nunca ejecutar `git push --force`.
- Nunca ejecutar `git reset --hard`.
- Nunca eliminar cambios del usuario.
- Nunca sobrescribir commits remotos.
- Nunca incorporar credenciales o información sensible.
- Nunca realizar commits vacíos.
- No modificar archivos ajenos a la tarea.
- No descartar cambios preexistentes sin autorización.

### Manejo de errores

Si existen conflictos, problemas de autenticación o errores durante el push:

- Detener la sincronización.
- Conservar todos los cambios locales.
- Informar claramente el problema.
- No afirmar que GitHub fue actualizado si el push falló.

### Informe al usuario

Al finalizar cada tarea, informar brevemente:

- Funcionalidad o modificación realizada.
- Pruebas ejecutadas y resultado.
- Hash del commit.
- Resultado de la sincronización con GitHub.

### Prioridad

La sincronización con GitHub forma parte del proceso normal de finalización de cada tarea de desarrollo. No esperar una solicitud adicional del usuario.
