import base64
import io
import cv2
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
from PIL import Image
import segmentation_models_pytorch as smp

# XAI - Grad-CAM
try:
    from pytorch_grad_cam import GradCAM
    from pytorch_grad_cam.utils.model_targets import SemanticSegmentationTarget
    import cv2
    XAI_AVAILABLE = True
    GRADCAM_ERROR = None
except ImportError:
    XAI_AVAILABLE = False
    GRADCAM_ERROR = "pytorch-grad-cam no instalado. Ejecuta: pip install grad-cam"

print(f"[model.py] XAI_AVAILABLE={XAI_AVAILABLE}, GRADCAM_ERROR={GRADCAM_ERROR}")


class CFG:
    img_size: int = 384
    num_classes: int = 4
    class_names: List[str] = [
        "Fondo_y_Sana",
        "Tizon",
        "Roya",
        "Mancha_Blanca",
    ]
    device: torch.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# Paleta de colores simple para visualizar las clases (RGB)
PALETTE: List[Tuple[int, int, int]] = [
    (0, 0, 0),
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
]


def build_model(device: Optional[torch.device] = None) -> torch.nn.Module:
    """Crea el modelo DeepLabV3+ para inferencia."""
    device = device or CFG.device
    model = smp.DeepLabV3Plus(
        encoder_name="mit_b1",
        encoder_weights="imagenet",
        classes=CFG.num_classes,
    )
    return model.to(device)


def load_model(
    weights_path: str = "modelofinal_bigdata.pth",
    device: Optional[torch.device] = None,
) -> torch.nn.Module:
    """Carga el modelo con los pesos guardados.

    Args:
        weights_path: Ruta al archivo .pth con los pesos.
        device: Dispositivo donde cargar el modelo.
    """
    device = device or CFG.device
    model = build_model(device)

    state = torch.load(weights_path, map_location=device)
    model.load_state_dict(state)
    model.eval()
    return model


def preprocess_image(image: Image.Image) -> torch.Tensor:
    """Preprocesa una imagen PIL para el modelo.

    - Redimensiona a CFG.img_size
    - Normaliza con los valores de imagenet usados por albumentations
    - Devuelve tensor en formato (1, C, H, W)
    """
    img = image.convert("RGB")
    img = img.resize((CFG.img_size, CFG.img_size), resample=Image.BILINEAR)
    img_arr = np.array(img).astype(np.float32) / 255.0

    # Albumentations Normalize predeterminado (ImageNet)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 1, 3)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 1, 3)

    img_arr = (img_arr - mean) / std
    img_arr = np.transpose(img_arr, (2, 0, 1)).astype(np.float32)
    tensor = torch.from_numpy(img_arr).unsqueeze(0).to(CFG.device)
    return tensor


def mask_to_image(mask: np.ndarray) -> Image.Image:
    """Convierte una máscara de clases en un PNG RGB con paleta."""
    h, w = mask.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    for cls_id, color in enumerate(PALETTE):
        rgb[mask == cls_id] = color
    return Image.fromarray(rgb)


def predict_from_bytes(
    model: torch.nn.Module,
    image_bytes: bytes,
    return_mask_image: bool = True,
) -> Dict[str, object]:
    """Realiza predicción y devuelve resultados útiles para la API."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    tensor = preprocess_image(image).to(CFG.device)

    with torch.no_grad():
        logits = model(tensor)
        preds = logits.argmax(dim=1).squeeze(0).cpu().numpy().astype(np.uint8)

    result: Dict[str, object] = {
        "classes": CFG.class_names,
        "class_counts": {name: int((preds == idx).sum()) for idx, name in enumerate(CFG.class_names)},
    }

    if return_mask_image:
        mask_img = mask_to_image(preds)
        buffer = io.BytesIO()
        mask_img.save(buffer, format="PNG")
        buffer.seek(0)
        encoded = base64.b64encode(buffer.read()).decode("utf-8")
        result["mask_base64_png"] = encoded

    return result


def generate_xai(
    model: torch.nn.Module,
    image_bytes: bytes,
    target_class: Optional[int] = None,
) -> Dict[str, object]:
    """Genera explicación con Grad-CAM para identificar observaciones importantes.
    
    Args:
        model: Modelo segmentación entrenado
        image_bytes: Imagen en bytes (PNG/JPG)
        target_class: Clase para explicar (si es None, usa clase dominante)
    
    Returns:
        Dict con heatmap en base64, clase explicada, confianza
    """
    print(f"[generate_xai] model.py __file__={__file__}, XAI_AVAILABLE={XAI_AVAILABLE}, GRADCAM_ERROR={GRADCAM_ERROR}")
    if not XAI_AVAILABLE:
        raise RuntimeError(GRADCAM_ERROR or "grad-cam no disponible")

    # Cargar y preparar imagen
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_rgb = np.array(image.resize((CFG.img_size, CFG.img_size))) / 255.0
    
    tensor = preprocess_image(image).to(CFG.device)

    # Obtener predicción
    with torch.no_grad():
        logits = model(tensor)
        preds = logits.argmax(dim=1).squeeze(0).cpu().numpy().astype(np.uint8)

    # Determinar clase objetivo (dominate sin contar fondo)
    if target_class is None:
        class_counts = [(idx, name, (preds == idx).sum()) for idx, name in enumerate(CFG.class_names) if idx != 0]
        class_counts = sorted(class_counts, key=lambda x: x[2], reverse=True)
        if len(class_counts) == 0 or class_counts[0][2] == 0:
            # Si no hay píxeles de ninguna clase de interés, fallback a fondo.
            target_class_idx = 0
            target_class_name = CFG.class_names[0]
        else:
            target_class_idx = class_counts[0][0]
            target_class_name = class_counts[0][1]
    else:
        target_class_idx = target_class
        target_class_name = CFG.class_names[target_class_idx] if target_class_idx < len(CFG.class_names) else "Unknown"

    # Generar Grad-CAM
    try:
        # Definir target layer (segmentation head)
        target_layers = [model.segmentation_head[0]]
        
        # Crear máscara binaria para la clase objetivo (mismo tamaño de salida del modelo 384x384)
        target_mask = (preds == target_class_idx).astype(np.uint8)
        targets = [SemanticSegmentationTarget(category=target_class_idx, mask=target_mask)]
        
        # Generar CAM
        with GradCAM(model=model, target_layers=target_layers) as cam:
            grayscale_cam = cam(input_tensor=tensor, targets=targets)[0, :]

        # Asegurar dimensiones (DeepLabV3+ reduce espacial) para pintar overlay
        if grayscale_cam.shape != (CFG.img_size, CFG.img_size):
            grayscale_cam = np.array(
                Image.fromarray((grayscale_cam * 255).astype(np.uint8))
                .resize((CFG.img_size, CFG.img_size), resample=Image.BILINEAR)
            ).astype(np.float32) / 255.0

        # Aplicar colormap vibrante (turbo) al heatmap
        heatmap_uint8 = (grayscale_cam * 255).astype(np.uint8)
        heatmap_color = cv2.applyColorMap(heatmap_uint8, cv2.COLORMAP_TURBO)
        heatmap_color = cv2.cvtColor(heatmap_color, cv2.COLOR_BGR2RGB)  # BGR a RGB
        heatmap_float = heatmap_color.astype(np.float32) / 255.0
        
        # Superponer heatmap sobre imagen con transparencia
        alpha = 0.6  # Opacidad del heatmap
        xai_vis = (1 - alpha) * img_rgb + alpha * heatmap_float
        
        # Codificar a base64
        xai_img = Image.fromarray((xai_vis * 255).astype(np.uint8))
        buffer = io.BytesIO()
        xai_img.save(buffer, format="PNG")
        buffer.seek(0)
        encoded = base64.b64encode(buffer.read()).decode("utf-8")
        
        # Confianza = proporción de píxeles para la clase objetivo
        confidence = float((preds == target_class_idx).sum()) / preds.size * 100
        
        return {
            "xai_base64_png": encoded,
            "target_class": target_class_name,
            "target_class_id": int(target_class_idx),
            "confidence": round(confidence, 2),
            "all_classes": CFG.class_names,
        }
    
    except Exception as e:
        return {"error": f"Error generando Grad-CAM: {str(e)}"}
