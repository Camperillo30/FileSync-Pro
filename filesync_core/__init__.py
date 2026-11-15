"""Core services shared by the FileSync Pro desktop application.

Paquete con la lógica "de bajo nivel" reutilizable por la app de escritorio
(Organizador.py), separada para poder probarla sin abrir ninguna ventana:

- `safety.py`: operaciones seguras de archivos (validación de rutas, escritura
  atómica, cuarentena de duplicados y su purga, borrado seguro).
- `platform_compat.py`: todo lo que cambia entre Windows y macOS (tipografías,
  carpeta de datos, recorrido de carpetas, identificador del equipo, Tesseract).
"""
