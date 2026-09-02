fn main() {
    // UI оболочки собирается Vite в ../app/dist (npm --prefix app run build) и
    // запекается в exe через generate_context!: frontendDist в tauri.conf.json.
    // Сборка без dist — честная ошибка, не пустое окно.
    println!("cargo:rerun-if-changed=../app/dist");
    tauri_build::build()
}
