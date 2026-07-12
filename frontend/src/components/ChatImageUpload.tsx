import React, { useRef } from 'react'
import { ImagePlus, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useToast } from '@/components/ui/toast'
import { ChatImage } from '@/stores/useChatStore'

interface ChatImageUploadProps {
  images: ChatImage[]
  onImagesChange: (images: ChatImage[]) => void
  maxImages?: number
  maxSizeBytes?: number
}

const generateImageId = () => `img_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`

export function ChatImageUpload({
  images,
  onImagesChange,
  maxImages = 3,
  maxSizeBytes = 5 * 1024 * 1024,
}: ChatImageUploadProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const { addToast } = useToast()

  const handleClick = () => {
    inputRef.current?.click()
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    if (!files.length) return

    if (images.length + files.length > maxImages) {
      addToast({
        title: '图片数量超限',
        description: `最多只能上传 ${maxImages} 张图片`,
        variant: 'warning',
      })
      e.target.value = ''
      return
    }

    files.forEach((file) => {
      if (!file.type.startsWith('image/')) {
        addToast({
          title: '不支持的文件格式',
          description: '请上传 PNG、JPEG、WebP 或 GIF 格式的图片',
          variant: 'warning',
        })
        return
      }
      if (file.size > maxSizeBytes) {
        addToast({
          title: '图片过大',
          description: '单张图片不能超过 5MB',
          variant: 'warning',
        })
        return
      }

      const reader = new FileReader()
      reader.onload = () => {
        const dataUrl = reader.result as string
        const newImage: ChatImage = {
          id: generateImageId(),
          name: file.name,
          dataUrl,
          size: file.size,
          type: file.type,
        }
        onImagesChange([...images, newImage])
      }
      reader.onerror = () => {
        addToast({
          title: '图片读取失败',
          description: '请重新选择图片',
          variant: 'destructive',
        })
      }
      reader.readAsDataURL(file)
    })

    e.target.value = ''
  }

  const handleRemove = (id: string) => {
    onImagesChange(images.filter((img) => img.id !== id))
  }

  return (
    <>
      {images.length < maxImages && (
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="h-10 w-10 rounded-lg"
          onClick={handleClick}
          aria-label="上传图片"
        >
          <ImagePlus className="h-5 w-5" />
        </Button>
      )}

      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        multiple
        className="hidden"
        onChange={handleFileChange}
      />
    </>
  )
}
