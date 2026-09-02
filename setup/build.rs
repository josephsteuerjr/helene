fn main() {
    // UI собирается Vite в ./dist (npm --prefix ui run build) и запекается в exe
    // через generate_context!. Сборка без dist — честная ошибка, не пустое окно.
    tauri_build::build()
}
