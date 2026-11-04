from contextlib import contextmanager

from django.db.models.signals import pre_save

from chat.models import Room


@contextmanager
def concurrent_room_update(**fields):
    """在 Room 存檔前一刻，模擬另一個請求改了同一列（例如房主剛輪替金鑰）。

    用來驗證 save(update_fields=...) 只寫自己負責的欄位，不會用手上的舊資料把別人的更新蓋掉。
    """
    def receiver(sender, instance, **kwargs):
        Room.objects.filter(pk=instance.pk).update(**fields)

    pre_save.connect(receiver, sender=Room)
    try:
        yield
    finally:
        pre_save.disconnect(receiver, sender=Room)
